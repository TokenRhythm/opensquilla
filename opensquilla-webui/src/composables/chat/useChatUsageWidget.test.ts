import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useChatUsageWidget } from './useChatUsageWidget'
import type { UsageReporting, UsageReportingRequestOptions } from '@/modules/usageReporting'
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
