/**
 * Transport-neutral ownership for a live Conversation event source.
 *
 * A source may be a WebSocket adapter today or an in-memory/replay source in
 * tests. The hub owns exactly one source subscription and fans messages out to
 * logical handles. Closing a handle therefore cannot close the physical
 * transport used by another handle (or by a reconnecting composition root).
 */

export interface ConversationEventSourceHandlers<TEvent> {
  onEvent?: (event: TEvent) => void
  onConnectionState?: (state: string) => void
  onDecodeError?: (error: unknown) => void
}

export interface ConversationEventSource<TEvent> {
  subscribe(handlers: ConversationEventSourceHandlers<TEvent>): () => void
}

export interface ConversationEventHandle<TEvent> {
  readonly key: string
  observe(listener: (event: TEvent) => void): () => void
  close(): void
}

export interface ConversationEventHub<TEvent> {
  /** Open a logical stream. An empty key means “all events”. */
  open(key: string): ConversationEventHandle<TEvent>
  /** Observe transport diagnostics without owning a logical stream. */
  observeConnectionState(listener: (state: string) => void): () => void
  observeDecodeError(listener: (error: unknown) => void): () => void
  /** Explicitly release the source and all logical handles. */
  dispose(): void
}

export interface ConversationEventHubOptions<TEvent> {
  /** Return the session identity carried by an event, or null if untagged. */
  sessionKey?: (event: TEvent) => string | null | undefined
}

type HandleState<TEvent> = {
  key: string
  listeners: Set<(event: TEvent) => void>
  closed: boolean
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
  let detachSource: (() => void) | null = null
  let disposed = false

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
        for (const handle of [...handles]) {
          if (handle.closed || !matches(handle, event)) continue
          for (const listener of [...handle.listeners]) listener(event)
        }
      },
      onConnectionState: (state) => {
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
    ) return
    detachSource?.()
    detachSource = null
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
      detachSource?.()
      detachSource = null
    },
  }
}
