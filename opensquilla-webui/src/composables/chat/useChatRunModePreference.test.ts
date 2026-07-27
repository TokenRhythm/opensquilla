// @vitest-environment happy-dom

import { describe, expect, it, vi, afterEach } from 'vitest'
import { effectScope, ref } from 'vue'

import {
  persistMaterializedSessionRunMode,
  RUN_MODE_STORAGE_KEY,
  useChatRunModePreference,
  type RunModePolicy,
} from './useChatRunModePreference'

function runInScope(policy: ReturnType<typeof ref<RunModePolicy | null>>) {
  const scope = effectScope()
  const api = scope.run(() => useChatRunModePreference({
    runModePolicy: () => policy.value,
  }))!
  return { api, scope }
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

  it('persists manual selections so the next mount keeps them', () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['standard', 'trusted', 'full'],
    })
    const first = runInScope(policy)

    first.api.setRunMode('standard')
    first.scope.stop()

    const second = runInScope(policy)
    expect(second.api.runMode.value).toBe('standard')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('standard')
    second.scope.stop()
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
