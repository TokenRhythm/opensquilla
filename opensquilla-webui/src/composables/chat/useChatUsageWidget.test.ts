import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatUsageWidget } from './useChatUsageWidget'
import type { RpcCallOptions } from '@/lib/rpc'

describe('useChatUsageWidget background reads', () => {
  it('uses the injected bounded options without changing its public loader', async () => {
    const readCallOptions: RpcCallOptions = {
      timeoutMs: 2_000,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    }
    const rpc = {
      waitForConnection: vi.fn().mockResolvedValue(undefined),
      call: vi.fn().mockResolvedValue({
        sessions: [{
          sessionKey: 'agent:main:webchat:usage',
          inputTokens: 12,
          outputTokens: 8,
        }],
      }),
    }
    const api = useChatUsageWidget({
      rpc,
      readCallOptions,
      sessionKey: ref('agent:main:webchat:usage'),
      tokenVizEnabled: () => false,
    })

    await api.loadCurrentSessionUsage()

    expect(rpc.waitForConnection).toHaveBeenCalledWith(
      2_000,
      undefined,
      {
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      },
    )
    expect(rpc.call).toHaveBeenCalledWith(
      'usage.status',
      { sessionKey: 'agent:main:webchat:usage' },
      readCallOptions,
    )
    expect(api.usageAccum.value).toMatchObject({ input: 12, output: 8 })
  })
})

describe('useChatUsageWidget context usage', () => {
  const SESSION = 'agent:main:webchat:context'

  async function loadWithContextStatus(contextStatus: Record<string, unknown> | null) {
    const api = useChatUsageWidget({
      rpc: {
        waitForConnection: vi.fn().mockResolvedValue(undefined),
        call: vi.fn().mockResolvedValue({
          sessions: [{ sessionKey: SESSION, contextStatus }],
        }),
      },
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
      (await loadWithContextStatus({ contextTokens: 54_000 })).contextUsage.value,
    ).toBeNull()
    expect(
      (await loadWithContextStatus({
        contextTokens: 54_000,
        contextWindowTokens: 0,
      })).contextUsage.value,
    ).toBeNull()
  })

  it('derives pressure from the counts when an older gateway omits it', async () => {
    const api = await loadWithContextStatus({
      context_tokens: 32_000,
      context_window_tokens: 128_000,
    })

    expect(api.contextUsage.value).toEqual({
      pct: 25,
      usedK: 32,
      windowK: 128,
      warning: false,
    })
  })

  it('keeps a fresh session at 0% instead of dropping the reading', async () => {
    // ``pressure: 0`` is a real measurement, not a missing one.
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
})
