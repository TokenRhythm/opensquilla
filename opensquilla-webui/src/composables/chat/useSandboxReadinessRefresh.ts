import { onScopeDispose, watch, type Ref } from 'vue'

import type { useSandboxSetupRecovery } from './useSandboxSetupRecovery'

interface SandboxReadinessRefreshOptions {
  connectionState: Readonly<Ref<string>>
  allowed: Readonly<Ref<boolean>>
  recovery: Pick<
    ReturnType<typeof useSandboxSetupRecovery>, 'status' | 'resolved' | 'loading' | 'refresh'
  >
}

export function useSandboxReadinessRefresh(options: SandboxReadinessRefreshOptions) {
  let requested = false
  let menuRefreshPending = false
  let disposed = false

  async function refreshIfNeeded(): Promise<void> {
    if (
      disposed
      || !requested
      || options.connectionState.value !== 'connected'
      || !options.allowed.value
    ) return
    if (options.recovery.status.value?.state === 'ready' || options.recovery.loading.value) {
      // A ready cache or an already-running read satisfies the menu request.
      menuRefreshPending = false
      return
    }
    if (options.recovery.resolved.value && !menuRefreshPending) return
    menuRefreshPending = false
    await options.recovery.refresh()
  }

  async function refreshAfterBootstrap(): Promise<void> {
    requested = true
    await refreshIfNeeded()
  }

  async function refreshOnOpen(): Promise<void> {
    // Opening the menu can recover a failed first read or observe setup that
    // completed in Settings. It never starts setup or changes the preference.
    if (options.recovery.status.value?.state !== 'ready') menuRefreshPending = true
    await refreshIfNeeded()
  }

  // A wake probe can recover the same socket without another connection event.
  // Retry the cleared readiness after that health transition, but let session
  // bootstrap close its admission gate before any optional RPC is queued.
  watch([options.connectionState, options.allowed], () => {
    if (requested) void refreshIfNeeded()
  }, { flush: 'post' })

  onScopeDispose(() => { disposed = true })

  return { refreshAfterBootstrap, refreshOnOpen }
}
