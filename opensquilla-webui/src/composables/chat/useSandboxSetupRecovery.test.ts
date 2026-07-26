import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, nextTick, ref } from 'vue'
import { useSandboxSetupRecovery } from './useSandboxSetupRecovery'

afterEach(() => {
  vi.useRealTimers()
})

function payload(state: string, platform = 'win32') {
  return { state, platform, message: state, requiresAdmin: false }
}

describe('useSandboxSetupRecovery', () => {
  it('hides ready status and never changes the selected run mode', async () => {
    const runMode = ref<'standard' | 'trusted' | 'full'>('trusted')
    const rpc = { call: vi.fn(async () => payload('ready')) }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode,
    }))!

    await vi.waitFor(() => expect(rpc.call).toHaveBeenCalledWith('sandbox.setup.status'))
    expect(recovery.status.value?.state).toBe('ready')
    expect(recovery.visible.value).toBe(false)
    expect(runMode.value).toBe('trusted')
    scope.stop()
  })

  it('short-polls setting_up until the setup becomes ready', async () => {
    vi.useFakeTimers()
    const rpc = {
      call: vi.fn()
        .mockResolvedValueOnce(payload('setting_up'))
        .mockResolvedValueOnce(payload('ready')),
    }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('standard'),
    }))!
    await vi.runAllTicks()
    await Promise.resolve()
    expect(recovery.status.value?.state).toBe('setting_up')
    expect(recovery.visible.value).toBe(true)

    await vi.advanceTimersByTimeAsync(2000)
    expect(recovery.status.value?.state).toBe('ready')
    expect(recovery.visible.value).toBe(false)
    scope.stop()
  })

  it('keeps short-polling after a transient status RPC failure', async () => {
    vi.useFakeTimers()
    const rpc = {
      call: vi.fn()
        .mockResolvedValueOnce(payload('setting_up'))
        .mockRejectedValueOnce(new Error('temporary status failure'))
        .mockResolvedValueOnce(payload('ready')),
    }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('standard'),
    }))!
    await vi.runAllTicks()
    await Promise.resolve()
    expect(recovery.status.value?.state).toBe('setting_up')

    await vi.advanceTimersByTimeAsync(2000)
    expect(rpc.call).toHaveBeenCalledTimes(2)
    expect(recovery.status.value?.state).toBe('setting_up')
    expect(recovery.error.value).toBe('temporary status failure')

    await vi.advanceTimersByTimeAsync(1999)
    expect(rpc.call).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(rpc.call).toHaveBeenCalledTimes(3)
    expect(recovery.status.value?.state).toBe('ready')
    expect(recovery.error.value).toBe('')
    expect(recovery.visible.value).toBe(false)
    scope.stop()
  })

  it('keeps short-polling after a malformed status payload', async () => {
    vi.useFakeTimers()
    const rpc = {
      call: vi.fn()
        .mockResolvedValueOnce(payload('setting_up'))
        .mockResolvedValueOnce({ state: 'future_state', platform: 'win32' })
        .mockResolvedValueOnce(payload('ready')),
    }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('standard'),
    }))!
    await vi.runAllTicks()
    await Promise.resolve()

    await vi.advanceTimersByTimeAsync(2000)
    expect(rpc.call).toHaveBeenCalledTimes(2)
    expect(recovery.status.value?.state).toBe('setting_up')

    await vi.advanceTimersByTimeAsync(2000)
    expect(rpc.call).toHaveBeenCalledTimes(3)
    expect(recovery.status.value?.state).toBe('ready')
    scope.stop()
  })

  it('does not poll an old Gateway again when no setup status was established', async () => {
    vi.useFakeTimers()
    const rpc = { call: vi.fn().mockRejectedValue(new Error('Method not found')) }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('standard'),
    }))!
    await vi.runAllTicks()
    await Promise.resolve()

    expect(rpc.call).toHaveBeenCalledTimes(1)
    expect(recovery.status.value).toBeNull()
    expect(recovery.visible.value).toBe(false)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(rpc.call).toHaveBeenCalledTimes(1)
    scope.stop()
  })

  it.each(['disconnected', 'full'] as const)(
    'does not let a late failed poll schedule work after becoming %s',
    async (inactiveBy) => {
      vi.useFakeTimers()
      let rejectPending: (cause: Error) => void = () => {}
      const pending = new Promise<unknown>((_resolve, reject) => { rejectPending = reject })
      const rpc = {
        call: vi.fn()
          .mockResolvedValueOnce(payload('setting_up'))
          .mockReturnValueOnce(pending),
      }
      const connectionState = ref('connected')
      const runMode = ref<'standard' | 'full'>('standard')
      const scope = effectScope()
      const recovery = scope.run(() => useSandboxSetupRecovery({ rpc, connectionState, runMode }))!
      await vi.runAllTicks()
      await Promise.resolve()

      await vi.advanceTimersByTimeAsync(2000)
      expect(rpc.call).toHaveBeenCalledTimes(2)
      if (inactiveBy === 'disconnected') connectionState.value = 'disconnected'
      else runMode.value = 'full'
      await nextTick()
      rejectPending(new Error('late status failure'))
      await Promise.resolve()
      await Promise.resolve()

      expect(recovery.status.value).toBeNull()
      expect(recovery.error.value).toBe('')
      await vi.advanceTimersByTimeAsync(10_000)
      expect(rpc.call).toHaveBeenCalledTimes(2)
      scope.stop()
    },
  )

  it('does not let a late failed poll schedule work after scope disposal', async () => {
    vi.useFakeTimers()
    let rejectPending: (cause: Error) => void = () => {}
    const pending = new Promise<unknown>((_resolve, reject) => { rejectPending = reject })
    const rpc = {
      call: vi.fn()
        .mockResolvedValueOnce(payload('setting_up'))
        .mockReturnValueOnce(pending),
    }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('standard'),
    }))!
    await vi.runAllTicks()
    await Promise.resolve()

    await vi.advanceTimersByTimeAsync(2000)
    expect(rpc.call).toHaveBeenCalledTimes(2)
    scope.stop()
    rejectPending(new Error('late status failure'))
    await Promise.resolve()
    await Promise.resolve()

    expect(recovery.error.value).toBe('')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(rpc.call).toHaveBeenCalledTimes(2)
  })

  it('offers owner setup only for Windows not_setup/failed states', async () => {
    const rpc = {
      call: vi.fn(async (method: string) =>
        method === 'sandbox.setup.ensure' ? payload('ready') : payload('not_setup')),
    }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('standard'),
    }))!
    await vi.waitFor(() => expect(recovery.canSetup.value).toBe(true))

    await recovery.ensureSetup()
    expect(rpc.call).toHaveBeenCalledWith('sandbox.setup.ensure')
    expect(recovery.status.value?.state).toBe('ready')
    expect(recovery.visible.value).toBe(false)
    scope.stop()
  })

  it('shows unavailable as explanation-only and resets dismissal on state/mode change', async () => {
    const runMode = ref<'standard' | 'trusted' | 'full'>('trusted')
    const connectionState = ref('connected')
    const rpc = { call: vi.fn(async () => payload('unavailable', 'darwin')) }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({ rpc, connectionState, runMode }))!
    await vi.waitFor(() => expect(recovery.visible.value).toBe(true))
    expect(recovery.canSetup.value).toBe(false)

    recovery.dismiss()
    expect(recovery.visible.value).toBe(false)
    runMode.value = 'standard'
    await vi.waitFor(() => expect(recovery.visible.value).toBe(true))
    expect(runMode.value).toBe('standard')
    scope.stop()
  })
})
