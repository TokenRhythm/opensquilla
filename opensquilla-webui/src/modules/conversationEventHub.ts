/**
 * Transport-neutral ownership for a live Conversation event source.
 *
 * A source may be a WebSocket adapter today or an in-memory/replay source in
 * tests. The hub owns exactly one source subscription and fans messages out to
 * logical handles. Closing a handle therefore cannot close the physical
 * transport used by another handle (or by a reconnecting composition root).
 */

export type ConversationConsumption = 'applied' | 'dirty'
export interface ConversationRecoveryScope {
  readonly keys: readonly string[]
  readonly global: boolean
}
export interface ConversationConsumptionContext {
  readonly revision: number
  isCurrent(): boolean
  /** Async consumers must use this gate immediately before mutating a view. */
  commit(write: () => void): boolean
}
// Existing observers may return incidental values (for example Array.push).
// Only the two explicit outcomes count as consumption proof.
export type ConversationEventListener<TEvent> = (event: TEvent, context?: ConversationConsumptionContext) => unknown

export interface ConversationEventSourceHandlers<TEvent> {
  onEvent?: ConversationEventListener<TEvent>
  onRecoveryRequired?: (scope: ConversationRecoveryScope) => Promise<boolean>
  onConnectionState?: (state: string) => void
  onDecodeError?: (error: unknown) => void
}

export interface ConversationEventSource<TEvent> {
  subscribe(handlers: ConversationEventSourceHandlers<TEvent>): () => void
}

export interface ConversationEventHandle<TEvent> {
  readonly key: string
  observe(listener: ConversationEventListener<TEvent>): () => void
  close(): void
}

export interface ConversationEventHub<TEvent> {
  /** Open a logical stream. An empty key means “all events”. */
  open(key: string): ConversationEventHandle<TEvent>
  /** Observe transport diagnostics without owning a logical stream. */
  observeConnectionState(listener: (state: string) => void): () => void
  observeDecodeError(listener: (error: unknown) => void): () => void
  observeRecoveryRequired(listener: (scope: ConversationRecoveryScope) => Promise<boolean>): () => void
  invalidateConsumption(key?: string): void
  /** Register a new full-bootstrap admission; call the returned gate only after remote release succeeds. */
  prepareReadRetirement(key: string): (released?: boolean) => void
  /** Explicitly release the source and all logical handles. */
  dispose(): void
}

export interface ConversationEventHubOptions<TEvent> {
  /** Return the session identity carried by an event, or null if untagged. */
  sessionKey?: (event: TEvent) => string | null | undefined
  /** Identity reset/deletion invalidates a retired logical key. */
  invalidatesSession?: (event: TEvent) => boolean
}

type HandleState<TEvent> = {
  key: string
  listeners: Set<ConversationEventListener<TEvent>>
  closed: boolean
  revision: number
}

const NOOP = () => {}

/**
 * Build a lazy, multiplexing event hub. The source is connected when the
 * first observer is attached and disconnected after the last observer/handle
 * is gone. All close/unsubscribe operations are idempotent.
 */
export function createConversationEventHub<TEvent>(
  source: ConversationEventSource<TEvent>,
  options: ConversationEventHubOptions<TEvent> = {},
): ConversationEventHub<TEvent> {
  const handles = new Set<HandleState<TEvent>>()
  const stateListeners = new Set<(state: string) => void>()
  const decodeErrorListeners = new Set<(error: unknown) => void>()
  const recoveryListeners = new Set<(scope: ConversationRecoveryScope) => Promise<boolean>>()
  const readAdmissions = new Map<string, symbol>()
  const retiredKeys = new Set<string>()
  let connectionRevision = 0
  let detachSource: (() => void) | null = null
  let disposed = false
  let consumptionRevision = 0

  function invalidateConsumption(key?: string) {
    consumptionRevision++
    for (const handle of handles) {
      if (!key || !handle.key || handle.key === key) handle.revision++
    }
  }

  function prepareReadRetirement(key: string): (released?: boolean) => void {
    invalidateConsumption(key)
    const admission = Symbol(key)
    const connection = connectionRevision
    readAdmissions.set(key, admission)
    retiredKeys.delete(key)
    return (released = true) => {
      if (readAdmissions.get(key) !== admission) return
      invalidateConsumption(key)
      readAdmissions.delete(key)
      if (!released || disposed || connection !== connectionRevision) return
      retiredKeys.add(key)
      // Retirement retains identities only, never a delta body or cursor.
      while (retiredKeys.size > 128) retiredKeys.delete(retiredKeys.values().next().value!)
    }
  }

  const matches = (handle: HandleState<TEvent>, event: TEvent): boolean => {
    if (!handle.key) return true
    const key = options.sessionKey?.(event)
    // Untagged legacy/task frames remain observable. This preserves v4's
    // historical wildcard behavior while positively fencing another session.
    return !key || key === handle.key
  }

  function ensureSource() {
    if (disposed || detachSource) return
    detachSource = source.subscribe({
      onEvent: (event) => {
        const key = options.sessionKey?.(event)
        if (key && options.invalidatesSession?.(event)) retiredKeys.delete(key)
        if (key && retiredKeys.has(key)) return 'applied'
        const results: Array<ReturnType<ConversationEventListener<TEvent>>> = []
        for (const handle of [...handles]) {
          if (handle.closed || !matches(handle, event)) continue
          for (const listener of [...handle.listeners]) {
            const revision = consumptionRevision
            const handleRevision = handle.revision
            const isCurrent = () => !disposed && !handle.closed && handle.listeners.has(listener)
              && handleRevision === handle.revision
            const context: ConversationConsumptionContext = {
              revision, isCurrent,
              commit(write) {
                if (!isCurrent()) return false
                write()
                return true
              },
            }
            const result = listener(event, context)
            results.push(result instanceof Promise ? result.then(value => {
              if (!isCurrent()) throw new Error('Conversation consumer was superseded.')
              return value
            }) : result)
          }
        }
        const classify = (values: unknown[]): ConversationConsumption | undefined =>
          values.includes('dirty') ? 'dirty' : values.includes('applied') ? 'applied' : undefined
        return results.some(result => result instanceof Promise)
          ? Promise.all(results).then(classify)
          : classify(results)
      },
      onRecoveryRequired: async scope => {
        const keys = [...new Set([...scope.keys, ...(scope.global ? readAdmissions.keys() : [])])]
        if (keys.length === 0) return false
        for (const key of keys) invalidateConsumption(key)
        // A global signal covers every actual read admission, never just the
        // visible handle. Each key independently needs an explicit owner.
        const results = await Promise.all(keys.map(async key => {
          if (retiredKeys.has(key)) return true
          if (!readAdmissions.has(key)) return false
          const owners = await Promise.all([...recoveryListeners].map(listener =>
            Promise.resolve().then(() => listener({ keys: [key], global: false })).catch(() => false)))
          return owners.some(Boolean)
        }))
        return results.every(Boolean)
      },
      onConnectionState: (state) => {
        if (state !== 'connected') {
          connectionRevision++
          retiredKeys.clear()
          invalidateConsumption()
        }
        for (const listener of [...stateListeners]) listener(state)
      },
      onDecodeError: (error) => {
        for (const listener of [...decodeErrorListeners]) listener(error)
      },
    })
  }

  function maybeDetachSource() {
    if (
      handles.size > 0
      || stateListeners.size > 0
      || decodeErrorListeners.size > 0
      || recoveryListeners.size > 0
    ) return
    detachSource?.()
    detachSource = null
    connectionRevision++
    retiredKeys.clear()
    invalidateConsumption()
  }

  function observeSet<TListener>(
    set: Set<TListener>,
    listener: TListener,
  ): () => void {
    if (disposed) return NOOP
    set.add(listener)
    ensureSource()
    let active = true
    return () => {
      if (!active) return
      active = false
      set.delete(listener)
      maybeDetachSource()
    }
  }

  function open(key: string): ConversationEventHandle<TEvent> {
    const state: HandleState<TEvent> = {
      key: String(key || ''),
      listeners: new Set(),
      closed: false,
      revision: 0,
    }
    handles.add(state)

    return {
      get key() { return state.key },
      observe(listener) {
        if (state.closed || disposed) return NOOP
        handles.add(state)
        state.listeners.add(listener)
        ensureSource()
        let active = true
        return () => {
          if (!active) return
          active = false
          state.listeners.delete(listener)
          if (state.listeners.size === 0) {
            handles.delete(state)
            maybeDetachSource()
          }
        }
      },
      close() {
        if (state.closed) return
        state.closed = true
        state.listeners.clear()
        handles.delete(state)
        maybeDetachSource()
      },
    }
  }

  return {
    open,
    observeConnectionState: listener => observeSet(stateListeners, listener),
    observeDecodeError: listener => observeSet(decodeErrorListeners, listener),
    observeRecoveryRequired: listener => observeSet(recoveryListeners, listener),
    invalidateConsumption,
    prepareReadRetirement,
    dispose() {
      if (disposed) return
      disposed = true
      for (const handle of handles) {
        handle.closed = true
        handle.listeners.clear()
      }
      handles.clear()
      stateListeners.clear()
      decodeErrorListeners.clear()
      recoveryListeners.clear()
      readAdmissions.clear()
      retiredKeys.clear()
      detachSource?.()
      detachSource = null
    },
  }
}
