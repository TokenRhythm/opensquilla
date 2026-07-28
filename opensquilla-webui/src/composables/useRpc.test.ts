// @vitest-environment happy-dom
import { createApp, defineComponent, h } from 'vue'
import { describe, expect, it, vi } from 'vitest'

const rpc = vi.hoisted(() => ({
  isConnected: true,
  call: vi.fn(async () => ({ audioConfigured: true })),
  on: vi.fn(() => () => {}),
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => rpc,
}))

import { useRpcCall } from '@/composables/useRpc'
import {
  acquireSessionBootstrapAdmission,
  optionalSessionRpcCallOptions,
} from '@/composables/chat/sessionBootstrapAdmission'

describe('useRpcCall session bootstrap admission', () => {
  it('defers and coalesces mount-time optional RPCs until recovery is terminal', async () => {
    rpc.call.mockClear()
    const release = acquireSessionBootstrapAdmission()
    const component = defineComponent({
      setup() {
        useRpcCall('onboarding.status', undefined, {
          callOptions: optionalSessionRpcCallOptions,
        })
        useRpcCall('onboarding.status', undefined, {
          callOptions: optionalSessionRpcCallOptions,
        })
        return () => h('div')
      },
    })
    const target = document.createElement('div')
    const app = createApp(component)
    app.mount(target)

    await Promise.resolve()
    expect(rpc.call).not.toHaveBeenCalled()

    release()
    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledOnce())
    expect(rpc.call).toHaveBeenCalledWith(
      'onboarding.status',
      undefined,
      optionalSessionRpcCallOptions,
    )
    app.unmount()
  })
})
