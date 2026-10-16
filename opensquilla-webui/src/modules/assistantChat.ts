import { retireAssistantChatPendingOwner } from './assistantChatPendingOwner'
import { SessionLifecycleError, type SessionLifecycle, type SidechatSession } from './sessionLifecycle'

/** The side adapter owns an ephemeral session, never a second chat runtime.
 * Reads, events, commands, receipts and rendering belong to the shared chat. */
export interface AssistantChatOwnerState {
  child: SidechatSession | null
  preparing: boolean
  closing: boolean
  error: string
  requiresNewSession: boolean
}

export interface AssistantChatOwnerOptions {
  identity: string
  parentKey: string
  ownerId: string
  lifecycle: SessionLifecycle
  isCurrent(): boolean
  /** Renewal belongs to the tab owner, including when its reader is hidden. */
  ownerAvailable(): boolean
  onChange(state: AssistantChatOwnerState): void
}

type OwnerIdentity = Pick<AssistantChatOwnerOptions, 'identity' | 'parentKey' | 'ownerId' | 'lifecycle'>
type OwnerLeaseOptions = Pick<AssistantChatOwnerOptions, 'parentKey' | 'ownerId' | 'lifecycle' | 'ownerAvailable'>
type OwnerLease = { options: OwnerLeaseOptions; timer?: ReturnType<typeof setTimeout>; lastRenewedAt: number }

// Bindings deliberately remain in memory. Opening/restoring a panel neither
// copies context nor creates a session; the first explicit send prepares it.
const creations = new Map<string, Promise<SidechatSession>>()
const bindings = new Map<string, SidechatSession>()
const closing = new Set<string>()
const ownerLeases = new Map<string, OwnerLease>()
const ownerFailures = new Map<string, Error>()
const ownerObservers = new Map<string, Set<(error: Error) => void>>()
const closingObservers = new Map<string, Set<(value: boolean) => void>>()
const OWNER_HEARTBEAT_MS = 60_000
const OWNER_LEASE_MS = 10 * 60_000

/** Compact namespace, not an authorization boundary. */
export function assistantChatBindingKey(identity: string, parentKey: string, ownerId = ''): string {
  let hash = 0xcbf29ce484222325n
  for (const byte of new TextEncoder().encode(JSON.stringify([identity, parentKey, ownerId]))) {
    hash = BigInt.asUintN(64, (hash ^ BigInt(byte)) * 0x100000001b3n)
  }
  return `opensquilla.assistant-chat.v1:${hash.toString(16)}`
}

function retireOwner(binding: string, owner: OwnerLease, error: Error) {
  if (ownerLeases.get(binding) !== owner) return
  retireAssistantChatPendingOwner(binding)
  if (owner.timer) clearTimeout(owner.timer)
  ownerLeases.delete(binding)
  ownerFailures.set(binding, error)
  for (const notify of ownerObservers.get(binding) || []) notify(error)
}

function renewOwner(binding: string, options: AssistantChatOwnerOptions) {
  if (ownerFailures.has(binding) || closing.has(binding)) return
  const leaseOptions: OwnerLeaseOptions = {
    parentKey: options.parentKey, ownerId: options.ownerId,
    lifecycle: options.lifecycle, ownerAvailable: options.ownerAvailable,
  }
  const existing = ownerLeases.get(binding)
  if (existing) { existing.options = leaseOptions; return }
  const owner: OwnerLease = { options: leaseOptions, lastRenewedAt: Date.now() }
  ownerLeases.set(binding, owner)
  const tick = async () => {
    if (ownerLeases.get(binding) !== owner) return
    if (Date.now() - owner.lastRenewedAt >= OWNER_LEASE_MS) {
      retireOwner(binding, owner, new SessionLifecycleError('invalid', 'The side conversation owner lease expired. Close this panel and open a new side conversation.'))
      return
    }
    if (closing.has(binding)) { schedule(); return }
    const current = owner.options
    if (current.ownerAvailable() && current.lifecycle.createSidechat) {
      try {
        const child = await current.lifecycle.createSidechat({ parentKey: current.parentKey, clientRequestId: current.ownerId })
        if (child.parentKey !== current.parentKey || child.key !== bindings.get(binding)?.key) {
          throw new SessionLifecycleError('invalid', 'The side conversation identity changed')
        }
        owner.lastRenewedAt = Date.now()
      } catch (error) {
        if (error instanceof SessionLifecycleError && ['invalid', 'not-found', 'forbidden', 'unsupported'].includes(error.code)) {
          retireOwner(binding, owner, error)
          return
        }
        // A transient failure consumes the remaining lease grace.
      }
    }
    if (ownerLeases.get(binding) === owner) schedule()
  }
  function schedule() {
    owner.timer = setTimeout(() => { void tick() }, OWNER_HEARTBEAT_MS)
    ;(owner.timer as unknown as { unref?: () => void }).unref?.()
  }
  schedule()
}

/** Explicit close is authoritative. Unmounting the adapter only detaches its
 * observer and must not cancel/delete a child or stop ownership renewal. */
export async function closeAssistantChatOwner(options: OwnerIdentity): Promise<void> {
  const binding = assistantChatBindingKey(options.identity, options.parentKey, options.ownerId)
  closing.add(binding)
  for (const notify of closingObservers.get(binding) || []) notify(true)
  try {
    const child = bindings.get(binding) || await creations.get(binding)
    if (child) {
      if (!options.lifecycle.closeSidechat) throw new Error('Side conversation close is unavailable')
      const result = await options.lifecycle.closeSidechat({ key: child.key })
      if (!result.closed || result.key !== child.key) throw new Error('Gateway did not close the side conversation')
    }
    bindings.delete(binding)
    retireAssistantChatPendingOwner(binding)
    const lease = ownerLeases.get(binding)
    if (lease?.timer) clearTimeout(lease.timer)
    ownerLeases.delete(binding)
    ownerFailures.delete(binding)
  } catch (error) {
    closing.delete(binding)
    for (const notify of closingObservers.get(binding) || []) notify(false)
    throw error
  }
}

export function createAssistantChatOwner(options: AssistantChatOwnerOptions) {
  const binding = assistantChatBindingKey(options.identity, options.parentKey, options.ownerId)
  const state: AssistantChatOwnerState = { child: null, preparing: false, closing: closing.has(binding), error: '', requiresNewSession: false }
  let disposed = false
  const current = () => !disposed && !closing.has(binding) && options.isCurrent()
  const notify = () => { if (!disposed && options.isCurrent()) options.onChange({ ...state }) }
  function fail(error: unknown) {
    state.error = error instanceof Error ? error.message : String(error)
    state.requiresNewSession ||= error instanceof SessionLifecycleError
      && ['invalid', 'not-found', 'forbidden', 'unsupported'].includes(error.code)
    notify()
  }
  const ownerFailed = (error: Error) => { state.requiresNewSession = true; fail(error) }
  const observers = ownerObservers.get(binding) || new Set<(error: Error) => void>()
  observers.add(ownerFailed)
  ownerObservers.set(binding, observers)
  const ownerClosing = (value: boolean) => { state.closing = value; notify() }
  const closeObservers = closingObservers.get(binding) || new Set<(value: boolean) => void>()
  closeObservers.add(ownerClosing)
  closingObservers.set(binding, closeObservers)

  function restore(): SidechatSession | null {
    if (!current()) return null
    const failure = ownerFailures.get(binding)
    if (failure) { ownerFailed(failure); return null }
    state.child = bindings.get(binding) || null
    if (state.child) renewOwner(binding, options)
    notify()
    return state.child
  }

  async function prepare(): Promise<SidechatSession | null> {
    if (!current() || state.requiresNewSession) return null
    const failure = ownerFailures.get(binding)
    if (failure) { ownerFailed(failure); return null }
    const saved = bindings.get(binding)
    if (saved) { state.child = saved; renewOwner(binding, options); notify(); return saved }
    state.preparing = true; state.error = ''; notify()
    try {
      if (!options.lifecycle.createSidechat) throw new SessionLifecycleError('unsupported', 'This Gateway does not support contextual side conversations')
      let creating = creations.get(binding)
      if (!creating) {
        creating = options.lifecycle.createSidechat({ parentKey: options.parentKey, clientRequestId: options.ownerId })
          .then(child => {
            if (!child.key || child.key === options.parentKey || child.parentKey !== options.parentKey) {
              throw new SessionLifecycleError('invalid', 'Invalid assistant session identity')
            }
            bindings.set(binding, child)
            renewOwner(binding, options)
            return child
          }).finally(() => creations.delete(binding))
        creations.set(binding, creating)
      }
      const child = await creating
      if (!current()) return null
      state.child = child
      return child
    } catch (error) {
      if (current()) fail(error)
      return null
    } finally {
      state.preparing = false
      notify()
    }
  }

  function dispose() {
    if (disposed) return
    disposed = true
    observers.delete(ownerFailed)
    if (!observers.size) ownerObservers.delete(binding)
    closeObservers.delete(ownerClosing)
    if (!closeObservers.size) closingObservers.delete(binding)
  }

  restore()
  return { binding, state, restore, prepare, dispose }
}
