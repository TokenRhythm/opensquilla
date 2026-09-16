import { describe, expect, it, vi } from 'vitest'
import { naturalRangeStartMs, requestUsageSnapshot } from './useUsageQuery'
import type { Observability } from '@/modules/observability'
import { usageSnapshot } from '@/testing/usage.test-helper'

describe('usage query presentation boundary', () => {
  it('uses local calendar boundaries rather than a rolling 24-hour window', () => {
    const now = new Date(2026, 6, 20, 15, 42, 31, 900)
    const start = new Date(naturalRangeStartMs('7', now)!)
    expect(start.getFullYear()).toBe(2026)
    expect(start.getMonth()).toBe(6)
    expect(start.getDate()).toBe(14)
    expect(start.getHours()).toBe(0)
    expect(start.getMinutes()).toBe(0)
    expect(start.getSeconds()).toBe(0)
    expect(naturalRangeStartMs('all', now)).toBeNull()
  })

  it('passes range, cancellation and cache to the domain without reinterpreting the result', async () => {
    const snapshot = usageSnapshot({ mode: 'ledger_partial' })
    const usage = vi.fn<Observability['usage']>().mockResolvedValue(snapshot)
    const unused = async (): Promise<never> => { throw new Error('Unexpected observation') }
    const observability: Observability = {
      usage, gatewayStatus: unused, selfLearningStatus: unused, readiness: unused,
      logStatus: unused, tailLogs: unused, updateNotice: unused, downloadSupportBundle: unused,
    }
    const options = {
      signal: new AbortController().signal,
      cachedSnapshot: snapshot,
      days: false, models: false, sessions: true, timezone: 'Asia/Shanghai',
      fallbackRange: 'today' as const,
    }
    expect(await requestUsageSnapshot(observability, '7', options)).toBe(snapshot)
    expect(usage).toHaveBeenCalledExactlyOnceWith('7', options)
  })
})
