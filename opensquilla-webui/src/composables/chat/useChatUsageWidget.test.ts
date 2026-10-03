import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useChatUsageWidget } from './useChatUsageWidget'
import type {
  UsageContextStatus,
  UsageReporting,
  UsageReportingRequestOptions,
} from '@/modules/usageReporting'
import { usageReportingDouble, usageSession, usageStatus } from '@/testing/usage.test-helper'

describe('useChatUsageWidget background reads', () => {
  it('uses injected bounded options and the canonical domain result', async () => {
    const readOptions: UsageReportingRequestOptions = {
      timeoutMs: 2_000, signal: new AbortController().signal,
    }
    const status = vi.fn<UsageReporting['status']>().mockResolvedValue(usageStatus({
      sessions: [{
        ...usageSession({
          sessionKey: 'agent:main:webchat:usage', inputTokens: 12, outputTokens: 8,
        }),
        contextStatus: null,
      }],
    }))
    const api = useChatUsageWidget({
      usageReporting: usageReportingDouble({ status }),
      readOptions,
      sessionKey: ref('agent:main:webchat:usage'),
      tokenVizEnabled: () => false,
    })

    await api.loadCurrentSessionUsage()

    expect(status).toHaveBeenCalledExactlyOnceWith('agent:main:webchat:usage', readOptions)
    expect(api.usageAccum.value).toMatchObject({
      input: 12, output: 8, cacheRead: 0, cacheWrite: 0, cost: null,
    })
  })

  it('clears previous usage and context warnings when the new session has no measurements', async () => {
    const status = vi.fn<UsageReporting['status']>()
      .mockResolvedValueOnce(usageStatus({
        sessions: [{
          ...usageSession({ sessionKey: 'session', inputTokens: 900, costUsd: 0.5 }),
          contextStatus: {
            contextTokens: 900, contextWindowTokens: 1000, pressure: 0.9, warningRatio: 0.85,
          },
        }],
      }))
      .mockResolvedValueOnce(usageStatus({
        sessions: [{ ...usageSession({ sessionKey: 'session' }), contextStatus: null }],
      }))
    const api = useChatUsageWidget({
      usageReporting: usageReportingDouble({ status }),
      sessionKey: ref('session'), tokenVizEnabled: () => false,
    })
    await api.loadCurrentSessionUsage()
    expect(api.usageAccum.value.cost).toBe(0.5)
    expect(api.contextWarning.value?.pct).toBe(90)
    await api.loadCurrentSessionUsage()
    expect(api.usageAccum.value).toMatchObject({ input: 0, output: 0, cost: null })
    expect(api.contextWarning.value).toBeNull()
  })
})

describe('useChatUsageWidget context usage', () => {
  const SESSION = 'agent:main:webchat:context'

  async function loadWithContextStatus(contextStatus: UsageContextStatus | null) {
    const status = vi.fn<UsageReporting['status']>().mockResolvedValue(usageStatus({
      sessions: [{ ...usageSession({ sessionKey: SESSION }), contextStatus }],
    }))
    const api = useChatUsageWidget({
      usageReporting: usageReportingDouble({ status }),
      sessionKey: ref(SESSION),
      tokenVizEnabled: () => false,
    })
    await api.loadCurrentSessionUsage()
    return api
  }

  it('reports usage well below the warning ratio', async () => {
    // The reading has to exist while the user can still act on it. Withheld
    // until 0.85 it could only ever announce an imminent compaction.
    const api = await loadWithContextStatus({
      contextTokens: 54_000,
      contextWindowTokens: 128_000,
      pressure: 0.42,
      warningRatio: 0.85,
    })

    expect(api.contextUsage.value).toEqual({
      pct: 42,
      usedK: 54,
      windowK: 128,
      warning: false,
    })
  })

  it('flags the warning at the gateway ratio rather than a second local one', async () => {
    const api = await loadWithContextStatus({
      contextTokens: 108_800,
      contextWindowTokens: 128_000,
      pressure: 0.85,
      warningRatio: 0.85,
    })

    expect(api.contextUsage.value?.warning).toBe(true)
    expect(api.contextUsage.value?.pct).toBe(85)
  })

  it('honours a gateway that moves its own warning ratio', async () => {
    const api = await loadWithContextStatus({
      contextTokens: 70_000,
      contextWindowTokens: 128_000,
      pressure: 0.55,
      warningRatio: 0.5,
    })

    expect(api.contextUsage.value?.warning).toBe(true)
  })

  it('stays null when the gateway resolved no window', async () => {
    // No denominator, no percentage: an invented one would read as measured.
    expect((await loadWithContextStatus(null)).contextUsage.value).toBeNull()
    expect(
      (await loadWithContextStatus({
        contextTokens: 54_000,
        contextWindowTokens: 0,
        pressure: 0,
        warningRatio: 0.85,
      })).contextUsage.value,
    ).toBeNull()
  })

  it('keeps a fresh session at 0% instead of dropping the reading', async () => {
    // `pressure: 0` is a real measurement, not a missing one.
    const api = await loadWithContextStatus({
      contextTokens: 0,
      contextWindowTokens: 128_000,
      pressure: 0,
      warningRatio: 0.85,
    })

    expect(api.contextUsage.value).toEqual({
      pct: 0,
      usedK: 0,
      windowK: 128,
      warning: false,
    })
  })

  it('keeps contextWarning as the above-threshold half of the same reading', async () => {
    // Main's existing consumers ask for the warning; they must not start
    // seeing a chip at 42% because the reading became always-on.
    const below = await loadWithContextStatus({
      contextTokens: 54_000, contextWindowTokens: 128_000, pressure: 0.42, warningRatio: 0.85,
    })
    expect(below.contextUsage.value).not.toBeNull()
    expect(below.contextWarning.value).toBeNull()

    const above = await loadWithContextStatus({
      contextTokens: 116_000, contextWindowTokens: 128_000, pressure: 0.9, warningRatio: 0.85,
    })
    expect(above.contextWarning.value).toEqual(above.contextUsage.value)
  })

  it('falls back to the quotient when the gateway sent no ratio at all', async () => {
    // `normalizeContextStatus` runs `pressure` through `finiteNumber`, whose
    // fallback is 0, so an omitted ratio arrives as a confident zero. Trusting
    // it would print "0%" next to a tooltip reading 115k / 128k.
    const api = await loadWithContextStatus({
      contextTokens: 115_000,
      contextWindowTokens: 128_000,
      pressure: 0,
      warningRatio: 0.85,
    })

    expect(api.contextUsage.value).toEqual({
      pct: 89,
      usedK: 115,
      windowK: 128,
      warning: true,
    })
  })

  it('floors the percentage so a partly full window never reads as full', async () => {
    // 99.5% rounds to 100, and a chip reading 100% says the window is gone.
    const api = await loadWithContextStatus({
      contextTokens: 127_360,
      contextWindowTokens: 128_000,
      pressure: 0.995,
      warningRatio: 0.85,
    })

    expect(api.contextUsage.value?.pct).toBe(99)
  })
})
