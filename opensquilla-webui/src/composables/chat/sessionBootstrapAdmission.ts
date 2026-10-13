import { computed, ref } from 'vue'

const activeHolds = ref(0)
let primedRelease: (() => void) | null = null
const viewOwners = new Set<symbol>()

/**
 * Optional, mount-time RPCs must not enter the Gateway's serialized dispatch
 * queue ahead of chat session recovery. ChatView acquires a hold synchronously
 * during setup (before child mounted hooks run) and releases it as soon as the
 * critical request frames have been queued.
 */
export const optionalSessionRpcAllowed = computed(() => activeHolds.value === 0)

export const OPTIONAL_SESSION_READ_TIMEOUT_MS = 10_000

export interface OptionalSessionReadOptions {
  readonly timeoutMs: number
  readonly signal?: AbortSignal
}

export const optionalSessionReadOptions: OptionalSessionReadOptions = {
  timeoutMs: OPTIONAL_SESSION_READ_TIMEOUT_MS,
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

/** Register a ChatView instance until its setup scope is disposed. */
export function registerSessionBootstrapAdmissionOwner(): () => void {
  const owner = Symbol('ChatView')
  viewOwners.add(owner)
  return () => { viewOwners.delete(owner) }
}

/**
 * Hold optional traffic while a lazy ChatView chunk is still resolving.
 *
 * Router navigation starts before App/Sidebar mounted hooks, so this closes
 * the otherwise-unavoidable gap where global metadata RPCs could enter the
 * Gateway's serial dispatcher before ChatView setup has a chance to run.
 * A retained ChatView (including one behind Settings) owns subsequent
 * bootstrap holds itself. Only a future view setup can claim a router prime.
 */
export function primeSessionBootstrapAdmission(): void {
  if (viewOwners.size > 0 || primedRelease) return
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
