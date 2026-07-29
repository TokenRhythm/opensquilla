// @vitest-environment happy-dom

import { describe, expect, it, vi, afterEach } from 'vitest'
import { effectScope, ref } from 'vue'

import {
  persistMaterializedSessionRunMode,
  RUN_MODE_STORAGE_KEY,
  useChatRunModePreference,
  type RunModePolicy,
} from './useChatRunModePreference'
import type { RpcCallOptions } from '@/lib/rpc'

function createRpc() {
  return {
    waitForConnection: vi.fn().mockResolvedValue(undefined),
    call: vi.fn().mockResolvedValue({ runMode: 'full', source: 'preference' }),
  }
}

function runInScope(
  policy: ReturnType<typeof ref<RunModePolicy | null>>,
  rpc = createRpc(),
  hydrateCallOptions?: RpcCallOptions,
) {
  const scope = effectScope()
  const api = scope.run(() => useChatRunModePreference({
    runModePolicy: () => policy.value,
    rpc,
    hydrateCallOptions,
  }))!
  return { api, scope, rpc }
}

afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('useChatRunModePreference', () => {
  it('uses policy default on a fresh browser with no saved user preference', () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['standard', 'trusted', 'full'],
    })

    const { api, scope } = runInScope(policy)

    expect(api.runMode.value).toBe('full')
    expect(api.runModeUserSelected.value).toBe(false)
    scope.stop()
  })

  it('restores the saved user preference instead of resetting to the policy default', () => {
    localStorage.setItem(RUN_MODE_STORAGE_KEY, 'trusted')
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['standard', 'trusted', 'full'],
    })

    const { api, scope } = runInScope(policy)

    expect(api.runMode.value).toBe('trusted')
    expect(api.runModeUserSelected.value).toBe(true)
    scope.stop()
  })

  it('hydrates from the backend and replaces a stale browser cache', async () => {
    localStorage.setItem(RUN_MODE_STORAGE_KEY, 'standard')
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['standard', 'trusted', 'full'],
    })
    const rpc = createRpc()
    const hydrateCallOptions: RpcCallOptions = {
      timeoutMs: 2_000,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    }
    rpc.call.mockResolvedValueOnce({ runMode: 'trusted', source: 'preference' })
    const { api, scope } = runInScope(policy, rpc, hydrateCallOptions)

    await api.hydrateRunModePreference()

    expect(rpc.call).toHaveBeenCalledWith(
      'sandbox.run_mode.preference.get',
      undefined,
      hydrateCallOptions,
    )
    expect(rpc.waitForConnection).toHaveBeenCalledWith(
      2_000,
      undefined,
      {
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      },
    )
    expect(api.runMode.value).toBe('trusted')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('trusted')
    scope.stop()
  })

  it('persists manual selections through the backend before updating cache', async () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['standard', 'trusted', 'full'],
    })
    const rpc = createRpc()
    rpc.call.mockResolvedValueOnce({ runMode: 'standard', source: 'preference' })
    const { api, scope } = runInScope(policy, rpc)

    const selected = await api.setGlobalRunMode('standard')

    expect(selected).toBe('standard')
    expect(rpc.call).toHaveBeenCalledWith('sandbox.run_mode.preference.set', {
      runMode: 'standard',
    })
    expect(api.runMode.value).toBe('standard')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('standard')
    scope.stop()
  })

  it('keeps the confirmed preference when a backend write fails', async () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['standard', 'trusted', 'full'],
    })
    const rpc = createRpc()
    rpc.call.mockRejectedValueOnce(new Error('write failed'))
    const { api, scope } = runInScope(policy, rpc)

    await expect(api.setGlobalRunMode('standard')).rejects.toThrow('write failed')

    expect(api.runMode.value).toBe('full')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBeNull()
    scope.stop()
  })

  it('applies a backend broadcast and coerces it to the principal policy', () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'trusted',
      allowedRunModes: ['standard', 'trusted'],
    })
    const { api, scope } = runInScope(policy)

    api.applyRunModePreferenceChanged({ runMode: 'full' })

    expect(api.runMode.value).toBe('trusted')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('trusted')
    scope.stop()
  })

  it('falls back when a saved preference is no longer allowed', () => {
    localStorage.setItem(RUN_MODE_STORAGE_KEY, 'full')
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'trusted',
      allowedRunModes: ['standard', 'trusted'],
    })

    const { api, scope } = runInScope(policy)

    expect(api.runMode.value).toBe('trusted')
    expect(api.runModeUserSelected.value).toBe(false)
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBeNull()
    scope.stop()
  })
})

describe('persistMaterializedSessionRunMode', () => {
  it('persists the selected mode for an existing session', async () => {
    const rpc = {
      waitForConnection: vi.fn().mockResolvedValue(undefined),
      call: vi.fn().mockResolvedValue({}),
    }

    await persistMaterializedSessionRunMode({
      rpc,
      sessionKey: 'agent:main:webchat:one',
      isDraft: false,
      runMode: 'standard',
    })

    expect(rpc.waitForConnection).toHaveBeenCalledOnce()
    expect(rpc.call).toHaveBeenCalledWith('sandbox.run_context.set', {
      sessionKey: 'agent:main:webchat:one',
      runMode: 'standard',
    })
  })

  it('does not create or mutate a session while the route is still a draft', async () => {
    const rpc = {
      waitForConnection: vi.fn().mockResolvedValue(undefined),
      call: vi.fn().mockResolvedValue({}),
    }

    await persistMaterializedSessionRunMode({
      rpc,
      sessionKey: 'agent:main:webchat:draft',
      isDraft: true,
      runMode: 'full',
    })

    expect(rpc.waitForConnection).not.toHaveBeenCalled()
    expect(rpc.call).not.toHaveBeenCalled()
  })
})
