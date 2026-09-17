import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'

import type {
  SandboxChatRuntime,
  SandboxReadinessState,
  SandboxSetupResult,
} from '@/modules/sandboxRuntime'
import type { SandboxSetupStatusPayload } from '@/types/sandbox'
import { useSandboxSetupRecovery } from './useSandboxSetupRecovery'

afterEach(() => {
  vi.useRealTimers()
})

function status(
  state: SandboxSetupStatusPayload['state'],
  platform = 'win32',
): SandboxSetupStatusPayload {
  return { state, platform, message: state, requiresAdmin: false }
}

function runtime(options: {
  readiness?: () => Promise<SandboxReadinessState>
  ensureReady?: () => Promise<SandboxSetupResult>
} = {}): Pick<SandboxChatRuntime, 'readiness' | 'ensureReady'> {
  return {
    readiness: vi.fn(options.readiness ?? (async () => ({
      status: status('ready'),
      capability: null,
    }))),
    ensureReady: vi.fn(options.ensureReady ?? (async () => ({
      ready: true,
      status: status('ready'),
      capability: null,
      outcome: 'ready' as const,
    }))),
  }
}

describe('useSandboxSetupRecovery', () => {
  it('can defer the first read until session bootstrap admits it', async () => {
    const sandbox = runtime()
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox,
      connectionState: ref('connected'),
      runMode: ref('safe'),
      autoRefresh: false,
    }))!

    await Promise.resolve()
    expect(sandbox.readiness).not.toHaveBeenCalled()
    await recovery.refresh()
    expect(sandbox.readiness).toHaveBeenCalledOnce()
    expect(recovery.resolved.value).toBe(true)
    scope.stop()
  })

  it('keeps an optional unsupported readiness projection passive', async () => {
    const sandbox = runtime({
      readiness: async () => ({ status: null, capability: null }),
    })
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox,
      connectionState: ref('connected'),
      runMode: ref('safe'),
    }))!

    await vi.waitFor(() => expect(recovery.resolved.value).toBe(true))
    expect(recovery.status.value).toBeNull()
    expect(recovery.visible.value).toBe(false)
    scope.stop()
  })

  it('hides ready status without changing the selected mode', async () => {
    const runMode = ref<'safe' | 'full'>('safe')
    const sandbox = runtime()
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox,
      connectionState: ref('connected'),
      runMode,
    }))!

    await vi.waitFor(() => expect(recovery.status.value?.state).toBe('ready'))
    expect(recovery.visible.value).toBe(false)
    expect(runMode.value).toBe('safe')
    scope.stop()
  })

  it('short-polls an authoritative setting_up state until ready', async () => {
    vi.useFakeTimers()
    const readiness = vi.fn()
      .mockResolvedValueOnce({ status: status('setting_up'), capability: null })
      .mockResolvedValueOnce({ status: status('ready'), capability: null })
    const sandbox = runtime({ readiness })
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox,
      connectionState: ref('connected'),
      runMode: ref('safe'),
    }))!

    await vi.runAllTicks()
    await Promise.resolve()
    expect(recovery.status.value?.state).toBe('setting_up')
    await vi.advanceTimersByTimeAsync(2_000)
    expect(recovery.status.value?.state).toBe('ready')
    expect(readiness).toHaveBeenCalledTimes(2)
    scope.stop()
  })

  it('keeps polling after a transient read failure once setup is in progress', async () => {
    vi.useFakeTimers()
    const readiness = vi.fn()
      .mockResolvedValueOnce({ status: status('setting_up'), capability: null })
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({ status: status('ready'), capability: null })
    const sandbox = runtime({ readiness })
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox,
      connectionState: ref('connected'),
      runMode: ref('safe'),
    }))!

    await vi.runAllTicks()
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(2_000)
    expect(recovery.error.value).toBe('temporary failure')
    expect(recovery.status.value?.state).toBe('setting_up')
    await vi.advanceTimersByTimeAsync(2_000)
    expect(recovery.status.value?.state).toBe('ready')
    expect(recovery.error.value).toBe('')
    scope.stop()
  })

  it('does not let a late failed poll revive work after disconnect', async () => {
    vi.useFakeTimers()
    let rejectPending!: (cause: Error) => void
    const pending = new Promise<SandboxReadinessState>((_resolve, reject) => {
      rejectPending = reject
    })
    const readiness = vi.fn()
      .mockResolvedValueOnce({ status: status('setting_up'), capability: null })
      .mockReturnValueOnce(pending)
    const connectionState = ref('connected')
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox: runtime({ readiness }),
      connectionState,
      runMode: ref('safe'),
    }))!

    await vi.runAllTicks()
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(2_000)
    connectionState.value = 'disconnected'
    await nextTick()
    rejectPending(new Error('late failure'))
    await Promise.resolve()
    await Promise.resolve()
    expect(recovery.status.value).toBeNull()
    expect(recovery.error.value).toBe('')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(readiness).toHaveBeenCalledTimes(2)
    scope.stop()
  })

  it('offers first-time setup only for the authoritative Windows state', async () => {
    const notSetup = status('not_setup')
    const sandbox = runtime({
      readiness: async () => ({ status: notSetup, capability: null }),
      ensureReady: async () => ({
        ready: true,
        status: status('ready'),
        capability: null,
        outcome: 'ready',
      }),
    })
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox,
      connectionState: ref('connected'),
      runMode: ref('safe'),
    }))!

    await vi.waitFor(() => expect(recovery.canSetup.value).toBe(true))
    await expect(recovery.ensureSetup()).resolves.toBe(true)
    expect(sandbox.ensureReady).toHaveBeenCalledOnce()
    expect(recovery.status.value?.state).toBe('ready')
    scope.stop()
  })

  it.each(['failed', 'unavailable', 'setting_up'] as const)(
    'does not offer setup for %s',
    async state => {
      const sandbox = runtime({
        readiness: async () => ({ status: status(state), capability: null }),
      })
      const scope = effectScope()
      const recovery = scope.run(() => useSandboxSetupRecovery({
        sandbox,
        connectionState: ref('connected'),
        runMode: ref('safe'),
      }))!

      await vi.waitFor(() => expect(recovery.resolved.value).toBe(true))
      expect(recovery.canSetup.value).toBe(false)
      await expect(recovery.ensureSetup()).resolves.toBe(false)
      expect(sandbox.ensureReady).not.toHaveBeenCalled()
      scope.stop()
    },
  )

  it('retains authoritative availability while Full Access hides recovery', async () => {
    const runMode = ref<'safe' | 'full'>('safe')
    const sandbox = runtime({
      readiness: async () => ({
        status: status('unavailable', 'darwin'),
        capability: null,
      }),
    })
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      sandbox,
      connectionState: ref('connected'),
      runMode,
    }))!

    await vi.waitFor(() => expect(recovery.visible.value).toBe(true))
    runMode.value = 'full'
    await nextTick()
    expect(recovery.status.value?.state).toBe('unavailable')
    expect(recovery.visible.value).toBe(false)
    scope.stop()
  })
})
