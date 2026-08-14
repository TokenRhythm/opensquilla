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
  it('can defer automatic status RPCs until the session bootstrap admits them', async () => {
    const rpc = { call: vi.fn(async () => payload('ready')) }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('safe'),
      autoRefresh: false,
    }))!

    await Promise.resolve()
    expect(rpc.call).not.toHaveBeenCalled()
    expect(recovery.resolved.value).toBe(false)
    await recovery.refresh()
    expect(rpc.call).toHaveBeenCalledOnce()
    expect(recovery.resolved.value).toBe(true)
    scope.stop()
  })

  it('resolves the initial check even when an old Gateway has no setup RPC', async () => {
    const rpc = { call: vi.fn().mockRejectedValue(new Error('Method not found')) }
    const connectionState = ref('connected')
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState,
      runMode: ref('safe'),
      autoRefresh: false,
    }))!

    expect(recovery.resolved.value).toBe(false)
    await recovery.refresh()
    expect(recovery.resolved.value).toBe(true)

    connectionState.value = 'disconnected'
    await nextTick()
    expect(recovery.resolved.value).toBe(false)
    scope.stop()
  })

  it('hides ready status and never changes the selected run mode', async () => {
    const runMode = ref<'safe' | 'full'>('safe')
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
    expect(runMode.value).toBe('safe')
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
      runMode: ref('safe'),
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
      runMode: ref('safe'),
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
      runMode: ref('safe'),
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
      runMode: ref('safe'),
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

  it(
    'does not let a late failed poll schedule work after disconnecting',
    async () => {
      vi.useFakeTimers()
      let rejectPending: (cause: Error) => void = () => {}
      const pending = new Promise<unknown>((_resolve, reject) => { rejectPending = reject })
      const rpc = {
        call: vi.fn()
          .mockResolvedValueOnce(payload('setting_up'))
          .mockReturnValueOnce(pending),
      }
      const connectionState = ref('connected')
      const runMode = ref<'safe' | 'full'>('safe')
      const scope = effectScope()
      const recovery = scope.run(() => useSandboxSetupRecovery({ rpc, connectionState, runMode }))!
      await vi.runAllTicks()
      await Promise.resolve()

      await vi.advanceTimersByTimeAsync(2000)
      expect(rpc.call).toHaveBeenCalledTimes(2)
      connectionState.value = 'disconnected'
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
      runMode: ref('safe'),
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
      runMode: ref('safe'),
    }))!
    await vi.waitFor(() => expect(recovery.canSetup.value).toBe(true))

    await recovery.ensureSetup()
    expect(rpc.call).toHaveBeenCalledWith('sandbox.setup.ensure')
    expect(recovery.status.value?.state).toBe('ready')
    expect(recovery.visible.value).toBe(false)
    scope.stop()
  })

  it('keeps authoritative availability while Full Access is selected', async () => {
    const runMode = ref<'safe' | 'full'>('safe')
    const connectionState = ref('connected')
    const rpc = { call: vi.fn(async () => payload('unavailable', 'darwin')) }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({ rpc, connectionState, runMode }))!
    await vi.waitFor(() => expect(recovery.visible.value).toBe(true))
    expect(recovery.canSetup.value).toBe(false)

    recovery.dismiss()
    expect(recovery.visible.value).toBe(false)
    runMode.value = 'full'
    await nextTick()
    expect(recovery.status.value?.state).toBe('unavailable')
    expect(recovery.visible.value).toBe(false)
    runMode.value = 'safe'
    await nextTick()
    expect(recovery.visible.value).toBe(true)
    expect(runMode.value).toBe('safe')
    scope.stop()
  })

  it('reports each terminal unavailable state once, including in Full Access', async () => {
    const onUnavailable = vi.fn()
    const rpc = { call: vi.fn(async () => payload('failed')) }
    const scope = effectScope()
    const recovery = scope.run(() => useSandboxSetupRecovery({
      rpc,
      connectionState: ref('connected'),
      runMode: ref('full'),
      onUnavailable,
    }))!

    await vi.waitFor(() => expect(onUnavailable).toHaveBeenCalledOnce())
    expect(recovery.status.value?.state).toBe('failed')
    expect(recovery.visible.value).toBe(false)

    await recovery.refresh()
    expect(onUnavailable).toHaveBeenCalledOnce()
    scope.stop()
  })
})
