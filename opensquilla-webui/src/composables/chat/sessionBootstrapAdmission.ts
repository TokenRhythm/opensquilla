import { computed, ref } from 'vue'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'

const activeHolds = ref(0)
let primedRelease: (() => void) | null = null

/**
 * Optional, mount-time RPCs must not enter the Gateway's serialized dispatch
 * queue ahead of chat session recovery. ChatView acquires a hold synchronously
 * during setup (before child mounted hooks run) and releases it as soon as the
 * critical request frames have been queued.
 */
export const optionalSessionRpcAllowed = computed(() => activeHolds.value === 0)

export const optionalSessionRpcCallOptions: RpcCallOptions = {
  timeoutMs: 2_000,
  // These methods still use the Gateway's serial dispatcher. Retire the
  // connection when one is abandoned so a stuck handler cannot keep later
  // navigation and control requests trapped behind it.
  timeoutAction: 'reconnect',
  abortAction: 'reconnect',
}

type OptionalSessionRpcClient = {
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<unknown>
}

export function waitForSessionRpcConnection(
  rpc: OptionalSessionRpcClient,
  callOptions?: RpcCallOptions,
): Promise<unknown> {
  if (!callOptions) return rpc.waitForConnection()
  return rpc.waitForConnection(
    callOptions.timeoutMs,
    callOptions.signal,
    {
      timeoutAction: callOptions.timeoutAction,
      abortAction: callOptions.abortAction,
    },
  )
}

function createSessionBootstrapAdmission(): () => void {
  activeHolds.value += 1
  let released = false
  return () => {
    if (released) return
    released = true
    activeHolds.value = Math.max(0, activeHolds.value - 1)
  }
}

export function acquireSessionBootstrapAdmission(): () => void {
  return createSessionBootstrapAdmission()
}

/**
 * Hold optional traffic while a lazy ChatView chunk is still resolving.
 *
 * Router navigation starts before App/Sidebar mounted hooks, so this closes
 * the otherwise-unavoidable gap where global metadata RPCs could enter the
 * Gateway's serial dispatcher before ChatView setup has a chance to run.
 * Priming is singleton/idempotent: query-only chat navigation reuses the
 * mounted ChatView and must not accumulate an owner nobody will claim.
 */
export function primeSessionBootstrapAdmission(): void {
  if (primedRelease) return
  primedRelease = createSessionBootstrapAdmission()
}

/**
 * Atomically transfers the router's pre-mount hold to ChatView.
 *
 * Returning the existing release function instead of releasing and acquiring
 * a new hold prevents optional watchers from observing a transient open gate.
 */
export function claimSessionBootstrapAdmission(): () => void {
  if (!primedRelease) return createSessionBootstrapAdmission()
  const release = primedRelease
  primedRelease = null
  return release
}

/** Release a navigation hold when the chat route is aborted or abandoned. */
export function clearPrimedSessionBootstrapAdmission(): void {
  const release = primedRelease
  primedRelease = null
  release?.()
}
