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
