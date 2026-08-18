import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatUsageWidget } from './useChatUsageWidget'
import type { RpcCallOptions } from '@/lib/rpc'

function makeRpc(sessions: unknown[] = []) {
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn().mockResolvedValue({ sessions }),
  }
}

describe('useChatUsageWidget background reads', () => {
  it('uses the injected bounded options without changing its public loader', async () => {
    const readCallOptions: RpcCallOptions = {
      timeoutMs: 2_000,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    }
    const rpc = makeRpc([{
      sessionKey: 'agent:main:webchat:usage',
      inputTokens: 12,
      outputTokens: 8,
    }])
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

describe('useChatUsageWidget context warning', () => {
  function contextStatus(status: Record<string, number>) {
    return {
      sessionKey: 'agent:main:webchat:usage',
      contextStatus: status,
    }
  }

  it('is null while no session or no context status is available', async () => {
    const api = useChatUsageWidget({
      rpc: makeRpc(),
      sessionKey: ref('agent:main:webchat:usage'),
      tokenVizEnabled: () => false,
    })
    await api.loadCurrentSessionUsage()
    expect(api.contextWarning.value).toBeNull()
  })

  it('reports the percentage below the warning ratio without a warning flag', async () => {
    const api = useChatUsageWidget({
      rpc: makeRpc([contextStatus({
        contextTokens: 54_000,
        contextWindowTokens: 128_000,
        pressure: 0.42,
        warningRatio: 0.85,
      })]),
      sessionKey: ref('agent:main:webchat:usage'),
      tokenVizEnabled: () => false,
    })
    await api.loadCurrentSessionUsage()
    expect(api.contextWarning.value).toEqual({
      pct: 42,
      usedK: 54,
      windowK: 128,
      warning: false,
    })
  })

  it('flags the warning once pressure crosses the ratio', async () => {
    const api = useChatUsageWidget({
      rpc: makeRpc([contextStatus({
        contextTokens: 111_000,
        contextWindowTokens: 128_000,
        pressure: 0.87,
        warningRatio: 0.85,
      })]),
      sessionKey: ref('agent:main:webchat:usage'),
      tokenVizEnabled: () => false,
    })
    await api.loadCurrentSessionUsage()
    expect(api.contextWarning.value).toEqual({
      pct: 87,
      usedK: 111,
      windowK: 128,
      warning: true,
    })
  })

  it('stays null when usage or the window is unknown', async () => {
    const api = useChatUsageWidget({
      rpc: makeRpc([contextStatus({
        contextTokens: 54_000,
        contextWindowTokens: 0,
        pressure: 0.42,
        warningRatio: 0.85,
      })]),
      sessionKey: ref('agent:main:webchat:usage'),
      tokenVizEnabled: () => false,
    })
    await api.loadCurrentSessionUsage()
    expect(api.contextWarning.value).toBeNull()
  })
})
