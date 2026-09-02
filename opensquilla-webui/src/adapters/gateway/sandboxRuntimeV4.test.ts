import { describe, expect, it, vi } from 'vitest'

import { createV4SandboxRuntime } from './sandboxRuntimeV4'

describe('createV4SandboxRuntime', () => {
  it('maps domain operations to the private sandbox wire methods', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'sandbox.policy.get') return { schemaVersion: 2, policyVersion: 0 }
      if (method === 'sandbox.run_mode.preference.set') return { runMode: 'safe', source: 'preference' }
      return { state: 'ready', platform: 'linux', message: 'ready', requiresAdmin: false }
    })
    const runtime = createV4SandboxRuntime({ request: request as never })

    await expect(runtime.policy()).resolves.toEqual({ schemaVersion: 2, policyVersion: 0 })
    await expect(runtime.setRunMode('safe')).resolves.toEqual({
      runMode: 'safe',
      source: 'preference',
    })
    expect(request).toHaveBeenNthCalledWith(1, 'sandbox.policy.get', undefined, expect.anything())
    expect(request).toHaveBeenNthCalledWith(
      2,
      'sandbox.run_mode.preference.set',
      { runMode: 'safe' },
      expect.anything(),
    )
  })

  it('normalizes malformed setup payloads to null and forwards event projections', async () => {
    const handler = vi.fn()
    const close = vi.fn()
    const runtime = createV4SandboxRuntime({
      request: vi.fn(async () => ({ nope: true })) as never,
      subscribe: vi.fn((_event, callback) => {
        callback({ runMode: 'safe', source: 'preference' })
        return { close }
      }),
    })

    await expect(runtime.setupStatus()).resolves.toBeNull()
    const unsubscribe = runtime.subscribeRunModePreferenceChanged(handler)
    expect(handler).toHaveBeenCalledWith({ runMode: 'safe', source: 'preference' })
    unsubscribe()
    expect(close).toHaveBeenCalledOnce()
  })
})
