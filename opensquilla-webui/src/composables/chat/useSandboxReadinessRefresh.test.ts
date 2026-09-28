import { afterEach, describe, expect, it, vi } from 'vitest'
import { computed, effectScope, nextTick, ref, watch } from 'vue'

import type { SandboxReadinessState } from '@/modules/sandboxRuntime'
import type { SandboxRunMode, SandboxSetupStatusPayload } from '@/types/sandbox'
import { allowedComposerRunModes, composerRunModeSelectionAction } from './composerRunMode'
import {
  acquireSessionBootstrapAdmission,
  optionalSessionRpcAllowed,
} from './sessionBootstrapAdmission'
import { useSandboxReadinessRefresh } from './useSandboxReadinessRefresh'
import { useSandboxSetupRecovery } from './useSandboxSetupRecovery'

const cleanup: Array<() => void> = []

afterEach(() => {
  for (const dispose of cleanup.splice(0).reverse()) dispose()
  vi.useRealTimers()
})

function readiness(state: SandboxSetupStatusPayload['state'] = 'ready'): SandboxReadinessState {
  return {
    status: { state, platform: 'darwin', message: state, requiresAdmin: false },
    capability: null,
  }
}

function holdBootstrap() {
  const release = acquireSessionBootstrapAdmission()
  cleanup.push(release)
  return release
}

function harness(read = vi.fn(async (): Promise<SandboxReadinessState> => readiness())) {
  const scope = effectScope()
  cleanup.push(() => scope.stop())
  return scope.run(() => {
    const connectionState = ref('connected')
    const runMode = ref<SandboxRunMode>('full')
    const ensureReady = vi.fn()
    const recovery = useSandboxSetupRecovery({
      sandbox: { readiness: read, ensureReady },
      connectionState,
      runMode,
      autoRefresh: false,
    })
    const refresh = useSandboxReadinessRefresh({
      connectionState,
      allowed: optionalSessionRpcAllowed,
      recovery,
    })
    const allowed = computed(() => allowedComposerRunModes(
      ['safe', 'full'], recovery.status.value, recovery.resolved.value,
    ))
    return { scope, connectionState, runMode, ensureReady, recovery, refresh, read, allowed }
  })!
}

async function flushRecovery() {
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

describe('useSandboxReadinessRefresh', () => {
  it('defers the first read until metadata requests it and bootstrap admits it', async () => {
    const release = holdBootstrap()
    const h = harness()
    h.connectionState.value = 'connecting'
    await nextTick()
    h.connectionState.value = 'connected'
    await nextTick()
    release()
    await nextTick()
    expect(h.read).not.toHaveBeenCalled()
    expect(h.allowed.value).toEqual(['full'])

    const releaseAgain = holdBootstrap()
    await h.refresh.refreshAfterBootstrap()
    expect(h.read).not.toHaveBeenCalled()
    releaseAgain()
    await flushRecovery()
    expect(h.read).toHaveBeenCalledOnce()
    expect(h.allowed.value).toEqual(['safe', 'full'])
  })

  it('restores Safe after a same-socket health recovery without another metadata callback', async () => {
    const h = harness()
    await h.refresh.refreshAfterBootstrap()
    expect(h.allowed.value).toEqual(['safe', 'full'])

    // Only ChatView's projected health state changes; no raw connection event
    // or bootstrap callback asks for a second read.
    h.connectionState.value = 'connecting'
    await nextTick()
    expect(h.recovery.resolved.value).toBe(false)
    expect(h.allowed.value).toEqual(['full'])
    h.connectionState.value = 'connected'
    await flushRecovery()

    expect(h.read).toHaveBeenCalledTimes(2)
    expect(h.allowed.value).toEqual(['safe', 'full'])
    expect(h.runMode.value).toBe('full')
    expect(h.ensureReady).not.toHaveBeenCalled()
  })

  it('does not let opening the menu start the first read before bootstrap', async () => {
    const h = harness()
    await h.refresh.refreshOnOpen()
    await nextTick()
    expect(h.read).not.toHaveBeenCalled()
    await h.refresh.refreshAfterBootstrap()
    expect(h.read).toHaveBeenCalledOnce()
    expect(h.allowed.value).toEqual(['safe', 'full'])
  })

  it.each(['error', 'empty'] as const)(
    'retries an initial %s result when the menu opens on the same healthy connection',
    async initial => {
      const h = harness()
      if (initial === 'error') h.read.mockRejectedValueOnce(new Error('Temporary read failure'))
      else h.read.mockResolvedValueOnce({ status: null, capability: null })
      await h.refresh.refreshAfterBootstrap()
      expect(h.recovery.resolved.value).toBe(true)
      expect(h.allowed.value).toEqual(['full'])
      expect(h.read).toHaveBeenCalledOnce()

      await h.refresh.refreshOnOpen()
      expect(h.connectionState.value).toBe('connected')
      expect(h.read).toHaveBeenCalledTimes(2)
      expect(h.allowed.value).toEqual(['safe', 'full'])
      expect(h.runMode.value).toBe('full')
      expect(h.ensureReady).not.toHaveBeenCalled()
    },
  )

  it.each(['not_setup', 'setting_up', 'failed', 'unavailable'] as const)(
    'rechecks a cached %s state on menu open after setup completes elsewhere',
    async state => {
      const h = harness()
      h.read.mockResolvedValueOnce(readiness(state))
      await h.refresh.refreshAfterBootstrap()
      expect(h.allowed.value).toEqual(['full'])
      await h.refresh.refreshOnOpen()
      expect(h.read).toHaveBeenCalledTimes(2)
      expect(h.allowed.value).toEqual(['safe', 'full'])
      expect(h.ensureReady).not.toHaveBeenCalled()
    },
  )

  it('keeps an explicit menu refresh pending while bootstrap admission is closed', async () => {
    const h = harness()
    h.read.mockResolvedValueOnce(readiness('unavailable'))
    await h.refresh.refreshAfterBootstrap()
    const release = holdBootstrap()
    await h.refresh.refreshOnOpen()
    await nextTick()
    expect(h.read).toHaveBeenCalledOnce()
    expect(h.allowed.value).toEqual(['full'])
    release()
    await flushRecovery()
    expect(h.read).toHaveBeenCalledTimes(2)
    expect(h.allowed.value).toEqual(['safe', 'full'])
  })

  it('does not reread an already-ready cache when the menu opens', async () => {
    const h = harness()
    await h.refresh.refreshAfterBootstrap()
    await h.refresh.refreshOnOpen()
    await h.refresh.refreshOnOpen()
    expect(h.read).toHaveBeenCalledOnce()
    expect(h.ensureReady).not.toHaveBeenCalled()
  })

  it('coalesces repeated menu opens while a non-null unavailable state is being refreshed', async () => {
    let resolveRead!: (value: SandboxReadinessState) => void
    const h = harness()
    h.read.mockResolvedValueOnce(readiness('unavailable'))
    await h.refresh.refreshAfterBootstrap()
    h.read.mockImplementationOnce(() => new Promise(resolve => { resolveRead = resolve }))
    const pending = h.refresh.refreshOnOpen()
    expect(h.recovery.status.value?.state).toBe('unavailable')
    expect(h.recovery.loading.value).toBe(true)
    await h.refresh.refreshOnOpen()
    await h.refresh.refreshAfterBootstrap()
    expect(h.read).toHaveBeenCalledTimes(2)
    expect(h.allowed.value).toEqual(['full'])
    resolveRead(readiness())
    await pending
    expect(h.allowed.value).toEqual(['safe', 'full'])
  })

  it('lets a real reconnect close bootstrap admission before requesting readiness', async () => {
    const order: string[] = []
    const h = harness(vi.fn(async () => {
      order.push('readiness')
      return readiness()
    }))
    await h.refresh.refreshAfterBootstrap()
    order.length = 0
    h.connectionState.value = 'disconnected'
    await nextTick()

    let release: (() => void) | undefined
    h.scope.run(() => watch(h.connectionState, state => {
      if (state === 'connected') {
        release = holdBootstrap()
        order.push('bootstrap hold')
      }
    }))
    h.connectionState.value = 'connected'
    await nextTick()
    await h.refresh.refreshAfterBootstrap()
    expect(order).toEqual(['bootstrap hold'])
    expect(h.read).toHaveBeenCalledOnce()
    expect(h.allowed.value).toEqual(['full'])

    order.push('subscribe queued', 'history queued')
    release!()
    await flushRecovery()
    expect(order).toEqual([
      'bootstrap hold', 'subscribe queued', 'history queued', 'readiness',
    ])
    expect(h.allowed.value).toEqual(['safe', 'full'])
  })

  it('coalesces metadata callbacks with a health recovery while the read is pending', async () => {
    let resolveRead!: (value: SandboxReadinessState) => void
    const h = harness()
    await h.refresh.refreshAfterBootstrap()
    h.read.mockImplementationOnce(() => new Promise(resolve => { resolveRead = resolve }))
    h.connectionState.value = 'connecting'
    await nextTick()
    h.connectionState.value = 'connected'
    await nextTick()
    expect(h.recovery.loading.value).toBe(true)
    expect(h.allowed.value).toEqual(['full'])
    await Promise.all([
      h.refresh.refreshAfterBootstrap(),
      h.refresh.refreshAfterBootstrap(),
    ])
    expect(h.read).toHaveBeenCalledTimes(2)

    resolveRead(readiness())
    await flushRecovery()
    await h.refresh.refreshAfterBootstrap()
    expect(h.read).toHaveBeenCalledTimes(2)
    expect(h.allowed.value).toEqual(['safe', 'full'])
  })

  it.each(['not_setup', 'setting_up', 'failed', 'unavailable'] as const)(
    'keeps Safe disabled when a recovered Mac reports %s',
    async state => {
      const h = harness()
      await h.refresh.refreshAfterBootstrap()
      h.read.mockResolvedValueOnce(readiness(state))
      h.connectionState.value = 'connecting'
      await nextTick()
      h.connectionState.value = 'connected'
      await flushRecovery()

      expect(h.recovery.status.value?.state).toBe(state)
      expect(h.allowed.value).toEqual(['full'])
      expect(composerRunModeSelectionAction(
        'safe', h.recovery.status.value, h.recovery.canSetup.value, h.recovery.resolved.value,
      )).toBe('ignore')
      expect(h.runMode.value).toBe('full')
      expect(h.ensureReady).not.toHaveBeenCalled()
    },
  )

  it('does not let a read from the old connection enable Safe after reconnect', async () => {
    let resolveOldRead!: (value: SandboxReadinessState) => void
    const h = harness(vi.fn(() => new Promise<SandboxReadinessState>(resolve => {
      resolveOldRead = resolve
    })))
    const firstRead = h.refresh.refreshAfterBootstrap()
    h.connectionState.value = 'disconnected'
    await nextTick()
    const release = holdBootstrap()
    h.connectionState.value = 'connected'
    await nextTick()
    resolveOldRead(readiness())
    await firstRead
    expect(h.recovery.resolved.value).toBe(false)
    expect(h.allowed.value).toEqual(['full'])

    h.read.mockResolvedValueOnce(readiness('unavailable'))
    release()
    await flushRecovery()
    expect(h.read).toHaveBeenCalledTimes(2)
    expect(h.recovery.status.value?.state).toBe('unavailable')
    expect(h.allowed.value).toEqual(['full'])
  })

  it('does not refresh after the view is disposed', async () => {
    const release = holdBootstrap()
    const h = harness()
    await h.refresh.refreshAfterBootstrap()
    h.scope.stop()
    release()
    await h.refresh.refreshAfterBootstrap()
    await nextTick()
    expect(h.read).not.toHaveBeenCalled()
  })
})
