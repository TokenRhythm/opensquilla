import { describe, expect, it, vi } from 'vitest'
import {
  naturalRangeStartMs,
  normalizeUsageQueryResponse,
  requestUsageSnapshot,
  usagePresetForRange,
  type UsageRpc,
} from './useUsageQuery'

function rpcWith(call: UsageRpc['call'], supports = true): UsageRpc {
  return {
    supportsMethod: vi.fn(() => supports),
    markMethodUnavailable: vi.fn(),
    waitForConnection: vi.fn(async () => {}),
    call,
  }
}

describe('usage.query compatibility client', () => {
  it('maps UI ranges to calendar-day presets', () => {
    expect(usagePresetForRange('today')).toBe('today')
    expect(usagePresetForRange('7')).toBe('last_7_calendar_days')
    expect(usagePresetForRange('14')).toBe('last_14_calendar_days')
    expect(usagePresetForRange('30')).toBe('last_30_calendar_days')
    expect(usagePresetForRange('all')).toBe('all')
  })

  it('uses local calendar boundaries rather than a rolling 24-hour window', () => {
    const now = new Date(2026, 6, 20, 15, 42, 31, 900)
    const start = new Date(naturalRangeStartMs('7', now)!)

    expect(start.getFullYear()).toBe(2026)
    expect(start.getMonth()).toBe(6)
    expect(start.getDate()).toBe(14)
    expect(start.getHours()).toBe(0)
    expect(start.getMinutes()).toBe(0)
    expect(start.getSeconds()).toBe(0)
  })

  it('requests schema v1 with the browser timezone and server dimensions', async () => {
    const call = vi.fn(async (method: string) => {
      expect(method).toBe('usage.query')
      return {
        schemaVersion: 1,
        source: 'usage_ledger',
        asOfMs: 123,
        range: { preset: 'last_7_calendar_days', timezone: 'Asia/Shanghai', fromMs: 1, toMs: 123 },
        totals: { inputTokens: 4, outputTokens: 5, costNanos: 9_200_000 },
        sessions: [],
        models: [],
        days: [],
        coverage: { status: 'complete' },
      }
    }) as UsageRpc['call']
    const rpc = rpcWith(call)

    const result = await requestUsageSnapshot(rpc, '7', { timezone: 'Asia/Shanghai' })

    expect(call).toHaveBeenCalledWith('usage.query', {
      schemaVersion: 1,
      range: { preset: 'last_7_calendar_days' },
      timezone: 'Asia/Shanghai',
      include: { days: true, models: true, sessions: true },
    })
    expect(result.mode).toBe('ledger_exact')
    expect(result.totals.cost).toBeCloseTo(0.0092, 9)
  })

  it('uses authoritative server totals instead of summing displayed sessions', () => {
    const result = normalizeUsageQueryResponse({
      schemaVersion: 1,
      totals: {
        inputTokens: 100,
        outputTokens: 50,
        costNanos: 30_000_000,
        sessionCount: 9,
        estimatedEventCount: 2,
        missingCostEntries: 1,
      },
      sessions: [{
        sessionKey: 'visible-only',
        totals: { inputTokens: 1, outputTokens: 2, costNanos: 3_000_000 },
      }],
      coverage: { status: 'complete' },
    })

    expect(result.sessions).toHaveLength(1)
    expect(result.totals).toMatchObject({
      input: 100,
      output: 50,
      cost: 0.03,
      sessions: 9,
      estimatedEventCount: 2,
      missingCostEntries: 1,
    })
  })

  it('normalizes additive native billing fields at every aggregation level', () => {
    const cny = {
      amount_nanos: '9007199254740993123',
      amount: '6.975',
      usd_equivalent_nanos: '1000000000',
      receipt_count: '2',
      normalization_rates_native_per_usd: ['6.975'],
    }
    const nativeTotals = {
      cost_nanos: '1000000000',
      cost_source: 'provider_billed',
      cost_source_counts: { provider_billed: '2' },
      native_billed_by_currency: { cny },
      pending_billing_receipt_count: '0',
      native_billing_expected_receipt_count: '2',
      native_billing_missing_confirmed_receipt_count: '0',
    }
    const result = normalizeUsageQueryResponse({
      schemaVersion: 1,
      totals: nativeTotals,
      sessions: [{
        session_key: 'native-session',
        totals: nativeTotals,
        model_breakdown: [{ model: 'tokenrhythm/model', totals: nativeTotals }],
      }],
      models: [{ provider: 'tokenrhythm', model: 'model', totals: nativeTotals }],
      days: [{ date: '2026-07-22', totals: nativeTotals }],
      coverage: {
        status: 'complete',
        native_billing: {
          status: 'partial',
          exact_from_ms: '456',
          reason_codes: ['native_billing_cutover'],
          missing_confirmed_receipt_count: '3',
          pending_receipt_count: '4',
        },
      },
    })

    const expected = {
      amountNanos: '9007199254740993123',
      amount: '6.975',
      usdEquivalentNanos: '1000000000',
      receiptCount: 2,
      normalizationRatesNativePerUsd: ['6.975'],
    }
    expect(result.totals.nativeBilledByCurrency?.CNY).toEqual(expected)
    expect(result.totals.nativeBillingExpectedReceiptCount).toBe(2)
    expect(result.totals.nativeBillingMissingConfirmedReceiptCount).toBe(0)
    expect(result.sessions[0].nativeBilledByCurrency?.CNY).toEqual(expected)
    expect(result.sessions[0].nativeBillingExpectedReceiptCount).toBe(2)
    expect(result.sessions[0].costSourceCounts).toEqual({ provider_billed: 2 })
    expect(result.sessions[0].modelBreakdown?.[0].nativeBilledByCurrency?.CNY).toEqual(expected)
    expect(result.sessions[0].modelBreakdown?.[0].costSourceCounts).toEqual({
      provider_billed: 2,
    })
    expect(result.models[0].nativeBilledByCurrency?.CNY).toEqual(expected)
    expect(result.models[0].costSourceCounts).toEqual({ provider_billed: 2 })
    expect(result.days[0].totals.nativeBilledByCurrency?.CNY).toEqual(expected)
    expect(result.coverage.nativeBilling).toEqual({
      status: 'partial',
      exactFromMs: 456,
      reasonCodes: ['native_billing_cutover'],
      missingConfirmedReceiptCount: 3,
      pendingReceiptCount: 4,
    })
  })

  it('keeps native billing unavailable for legacy responses', () => {
    const result = normalizeUsageQueryResponse({
      schemaVersion: 1,
      totals: { costNanos: 1 },
      coverage: { status: 'complete' },
    })

    expect(result.totals.nativeBilledByCurrency).toEqual({})
    expect(result.totals.pendingBillingReceiptCount).toBe(0)
    expect(result.fxRatesNativePerUsd).toEqual({})
    expect(result.coverage.nativeBilling).toEqual({
      status: 'unavailable',
      exactFromMs: null,
      reasonCodes: [],
      missingConfirmedReceiptCount: 0,
      pendingReceiptCount: 0,
    })
  })

  it('normalizes served fx display rates and drops malformed entries', () => {
    const result = normalizeUsageQueryResponse({
      schemaVersion: 1,
      totals: { costNanos: 1 },
      coverage: { status: 'complete' },
      fxRatesNativePerUsd: {
        cny: '6.975',
        JPY: 'not-a-rate',
        EUR: '0',
      },
    })

    expect(result.fxRatesNativePerUsd).toEqual({ CNY: '6.975' })

    const snakeCase = normalizeUsageQueryResponse({
      schemaVersion: 1,
      totals: { costNanos: 1 },
      coverage: { status: 'complete' },
      fx_rates_native_per_usd: { CNY: '6.975' },
    })

    expect(snakeCase.fxRatesNativePerUsd).toEqual({ CNY: '6.975' })
  })

  it('does not turn a deleted session id into a clickable session key', () => {
    const result = normalizeUsageQueryResponse({
      schemaVersion: 1,
      totals: { costNanos: 1 },
      sessions: [{
        sessionId: 'deleted-session-id',
        sessionKey: null,
        totals: { costNanos: 1 },
      }],
      coverage: { status: 'complete' },
    })

    expect(result.sessions[0]).toMatchObject({
      session: 'deleted-session-id',
      sessionId: 'deleted-session-id',
      sessionKey: '',
    })
  })

  it('treats partial coverage as a successful ledger response', async () => {
    const call = vi.fn(async () => ({
      schemaVersion: 1,
      totals: { costNanos: 8_000_000 },
      coverage: {
        status: 'partial',
        reasonCodes: ['backfill_running', 'legacy_unattributed'],
        legacyUnattributed: { includedInTotals: true, totals: { costNanos: 2_000_000 } },
      },
      sessions: [],
    })) as UsageRpc['call']

    const result = await requestUsageSnapshot(rpcWith(call), 'all', { timezone: 'UTC' })

    expect(result.mode).toBe('ledger_partial')
    expect(result.coverage.reasonCodes).toContain('backfill_running')
    expect(result.coverage.legacyIncludedInTotals).toBe(true)
    expect(call).toHaveBeenCalledTimes(1)
  })

  it('does not probe usage.query when Hello does not advertise it', async () => {
    const call = vi.fn(async (method: string) => {
      expect(method).toBe('usage.status')
      return { totalSessions: 2, totalTokens: 7, totalCostUsd: 0.4, sessions: [] }
    }) as UsageRpc['call']

    const result = await requestUsageSnapshot(rpcWith(call, false), 'all', { timezone: 'UTC' })

    expect(call).toHaveBeenCalledTimes(1)
    expect(result.mode).toBe('session_approximation')
    expect(result.totals).toMatchObject({ sessions: 2, totalTokens: 7, cost: 0.4 })
  })

  it('silently disables a missing method for the connection and falls back', async () => {
    const missing = Object.assign(new Error('Method not found: usage.query'), { code: 'METHOD_NOT_FOUND' })
    const call = vi.fn(async (method: string) => {
      if (method === 'usage.query') throw missing
      return { sessions: [] }
    }) as UsageRpc['call']
    const rpc = rpcWith(call)

    const result = await requestUsageSnapshot(rpc, '7', { timezone: 'UTC' })

    expect(rpc.markMethodUnavailable).toHaveBeenCalledWith('usage.query')
    expect(call).toHaveBeenNthCalledWith(1, 'usage.query', expect.any(Object))
    expect(call).toHaveBeenNthCalledWith(2, 'usage.status')
    expect(result.mode).toBe('session_approximation')
  })

  it('retries an invalid browser timezone once with UTC', async () => {
    let queryAttempts = 0
    const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      expect(method).toBe('usage.query')
      queryAttempts += 1
      if (queryAttempts === 1) throw new Error('Invalid timezone: Mars/Olympus')
      expect(params?.timezone).toBe('UTC')
      return {
        schemaVersion: 1,
        range: { preset: 'today', timezone: 'UTC' },
        totals: { costNanos: 1_000_000 },
        coverage: { status: 'complete' },
      }
    }) as UsageRpc['call']

    const result = await requestUsageSnapshot(rpcWith(call), 'today', {
      timezone: 'Mars/Olympus',
    })

    expect(call).toHaveBeenCalledTimes(2)
    expect(call).toHaveBeenNthCalledWith(1, 'usage.query', expect.objectContaining({
      timezone: 'Mars/Olympus',
    }))
    expect(call).toHaveBeenNthCalledWith(2, 'usage.query', expect.objectContaining({
      timezone: 'UTC',
    }))
    expect(result.source).toBe('usage_ledger')
    expect(result.timezone).toBe('UTC')
    expect(result.timezoneFallback).toEqual({
      requestedTimezone: 'Mars/Olympus',
      effectiveTimezone: 'UTC',
      reason: 'invalid_timezone',
    })
  })

  it('keeps the same-range ledger cache when query and status both fail', async () => {
    const cached = normalizeUsageQueryResponse({
      schemaVersion: 1,
      range: { preset: 'last_7_calendar_days', timezone: 'Asia/Shanghai' },
      totals: { costNanos: 9_200_000 },
      coverage: { status: 'complete' },
    })
    const call = vi.fn(async (method: string) => {
      throw new Error(method === 'usage.query' ? 'query temporarily unavailable' : 'gateway busy')
    }) as UsageRpc['call']

    const result = await requestUsageSnapshot(rpcWith(call), '7', {
      timezone: 'Asia/Shanghai',
      cachedSnapshot: cached,
    })

    expect(call).toHaveBeenNthCalledWith(1, 'usage.query', expect.any(Object))
    expect(call).toHaveBeenNthCalledWith(2, 'usage.status')
    expect(result).toBe(cached)
  })

  it('does not replace an exact cache with an approximation after a transient query failure', async () => {
    const cached = normalizeUsageQueryResponse({
      schemaVersion: 1,
      range: { preset: 'all', timezone: 'UTC' },
      totals: { costNanos: 9_200_000 },
      coverage: { status: 'complete' },
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'usage.query') throw new Error('query temporarily unavailable')
      return { totalCostUsd: 99, sessions: [] }
    }) as UsageRpc['call']

    const result = await requestUsageSnapshot(rpcWith(call), 'all', {
      timezone: 'UTC',
      cachedSnapshot: cached,
    })

    expect(result).toBe(cached)
    expect(result.totals.cost).toBeCloseTo(0.0092, 9)
  })
})
