import { effectScope, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useChatUsageWidget } from './useChatUsageWidget'
import type { UsageReporting, UsageReportingRequestOptions } from '@/modules/usageReporting'
import { usageReportingDouble, usageSession, usageStatus } from '@/testing/usage.test-helper'

function deferredUsage() {
  let resolve!: (value: Awaited<ReturnType<UsageReporting['status']>>) => void
  const promise = new Promise<Awaited<ReturnType<UsageReporting['status']>>>(done => { resolve = done })
  return { promise, resolve }
}

function measuredUsage(sessionKey: string, inputTokens: number) {
  return usageStatus({ sessions: [{ ...usageSession({ sessionKey, inputTokens, costUsd: inputTokens / 1000 }), contextStatus: null }] })
}

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

  it('does not replace the completed-turn usage with an older background response', async () => {
    const old = deferredUsage()
    const status = vi.fn<UsageReporting['status']>()
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce(measuredUsage('session', 20))
    const api = useChatUsageWidget({
      usageReporting: usageReportingDouble({ status }),
      sessionKey: ref('session'), tokenVizEnabled: () => false,
    })
    const pending = api.loadCurrentSessionUsage()
    await api.loadCurrentSessionUsage()
    old.resolve(measuredUsage('session', 5)); await pending
    expect(api.usageAccum.value).toMatchObject({ input: 20, cost: 0.02 })
  })

  it('does not apply pending usage after the conversation is disposed', async () => {
    const response = deferredUsage(), scope = effectScope()
    const status = vi.fn<UsageReporting['status']>().mockReturnValue(response.promise)
    const api = scope.run(() => useChatUsageWidget({
      usageReporting: usageReportingDouble({ status }),
      sessionKey: ref('session'), tokenVizEnabled: () => false,
    }))!
    const pending = api.loadCurrentSessionUsage()
    scope.stop()
    response.resolve(measuredUsage('session', 20)); await pending
    expect(api.usageAccum.value).toMatchObject({ input: 0, cost: null })
    await api.loadCurrentSessionUsage()
    expect(status).toHaveBeenCalledOnce()
  })

  it.each(['session', 'connection'] as const)('retires pending usage when the %s identity changes and returns', async change => {
    const response = deferredUsage(), scope = effectScope()
    const sessionKey = ref('session-a'), connection = ref('gateway-a')
    const status = vi.fn<UsageReporting['status']>().mockReturnValue(response.promise)
    const api = scope.run(() => useChatUsageWidget({
      usageReporting: usageReportingDouble({ status }), sessionKey,
      connectionIdentity: () => connection.value, tokenVizEnabled: () => false,
    }))!
    const pending = api.loadCurrentSessionUsage()
    if (change === 'session') {
      sessionKey.value = 'session-b'; sessionKey.value = 'session-a'
    } else {
      connection.value = 'gateway-b'; connection.value = 'gateway-a'
    }
    response.resolve(measuredUsage('session-a', 20)); await pending
    expect(api.usageAccum.value).toMatchObject({ input: 0, cost: null })
    scope.stop()
  })
})
