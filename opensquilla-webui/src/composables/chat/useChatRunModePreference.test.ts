// @vitest-environment happy-dom

import { describe, expect, it, vi, afterEach } from 'vitest'
import { effectScope, ref } from 'vue'

import {
  RUN_MODE_STORAGE_KEY,
  useChatRunModePreference,
  type RunModePolicy,
} from './useChatRunModePreference'

function createSandbox() {
  return {
    preference: vi.fn().mockResolvedValue({ runMode: 'full' as const, source: 'preference' }),
    selectMode: vi.fn().mockResolvedValue({ runMode: 'full' as const, source: 'preference' }),
  }
}

function runInScope(
  policy: ReturnType<typeof ref<RunModePolicy | null>>,
  sandbox = createSandbox(),
) {
  const scope = effectScope()
  const api = scope.run(() => useChatRunModePreference({
    runModePolicy: () => policy.value,
    sandbox,
  }))!
  return { api, scope, sandbox }
}

afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('useChatRunModePreference', () => {
  it('starts in Full Access before the principal policy arrives', () => {
    const policy = ref<RunModePolicy | null>(null)

    const { api, scope } = runInScope(policy)

    expect(api.runMode.value).toBe('full')
    expect(api.runModeUserSelected.value).toBe(false)
    scope.stop()
  })

  it('uses policy default on a fresh browser with no saved user preference', () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['safe', 'full'],
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
      allowedRunModes: ['safe', 'full'],
    })

    const { api, scope } = runInScope(policy)

    expect(api.runMode.value).toBe('safe')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('safe')
    expect(api.runModeUserSelected.value).toBe(true)
    scope.stop()
  })

  it('hydrates from the backend and replaces a stale browser cache', async () => {
    localStorage.setItem(RUN_MODE_STORAGE_KEY, 'standard')
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['safe', 'full'],
    })
    const sandbox = createSandbox()
    sandbox.preference.mockResolvedValueOnce({ runMode: 'safe', source: 'preference' })
    const { api, scope } = runInScope(policy, sandbox)

    await api.hydrateRunModePreference()

    expect(sandbox.preference).toHaveBeenCalledWith({ timeoutMs: 10_000 })
    expect(api.runMode.value).toBe('safe')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('safe')
    scope.stop()
  })

  it('persists manual selections through the backend before updating cache', async () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['safe', 'full'],
    })
    const sandbox = createSandbox()
    sandbox.selectMode.mockResolvedValueOnce({ runMode: 'safe', source: 'preference' })
    const { api, scope } = runInScope(policy, sandbox)

    const selected = await api.setGlobalRunMode('safe')

    expect(selected).toBe('safe')
    expect(sandbox.selectMode).toHaveBeenCalledWith('safe', { timeoutMs: 5_000 })
    expect(api.runMode.value).toBe('safe')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('safe')
    scope.stop()
  })

  it('updates the visible selection immediately while persistence is pending', async () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['safe', 'full'],
    })
    const sandbox = createSandbox()
    let resolveWrite!: (payload: unknown) => void
    sandbox.selectMode.mockReturnValueOnce(new Promise(resolve => {
      resolveWrite = resolve
    }))
    const { api, scope } = runInScope(policy, sandbox)

    const pending = api.setGlobalRunMode('safe')

    expect(api.runMode.value).toBe('safe')
    await Promise.resolve()
    expect(sandbox.selectMode).toHaveBeenCalledWith('safe', { timeoutMs: 5_000 })

    resolveWrite({ runMode: 'safe', source: 'preference' })
    await expect(pending).resolves.toBe('safe')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('safe')
    scope.stop()
  })

  it('keeps the confirmed preference when a backend write fails', async () => {
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'full',
      allowedRunModes: ['safe', 'full'],
    })
    const sandbox = createSandbox()
    sandbox.selectMode.mockRejectedValueOnce(new Error('write failed'))
    const { api, scope } = runInScope(policy, sandbox)

    await expect(api.setGlobalRunMode('safe')).rejects.toThrow('write failed')

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

    expect(api.runMode.value).toBe('safe')
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBe('safe')
    scope.stop()
  })

  it('falls back when a saved preference is no longer allowed', () => {
    localStorage.setItem(RUN_MODE_STORAGE_KEY, 'full')
    const policy = ref<RunModePolicy | null>({
      defaultRunMode: 'trusted',
      allowedRunModes: ['standard', 'trusted'],
    })

    const { api, scope } = runInScope(policy)

    expect(api.runMode.value).toBe('safe')
    expect(api.runModeUserSelected.value).toBe(false)
    expect(localStorage.getItem(RUN_MODE_STORAGE_KEY)).toBeNull()
    scope.stop()
  })
})
