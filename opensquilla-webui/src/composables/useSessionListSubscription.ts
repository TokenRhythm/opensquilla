import type { ConnectionState, RpcEventHandler } from '@/lib/rpc'

type SessionListRpc = {
  call(method: string, params?: Record<string, unknown>): Promise<unknown>
  on(event: string, handler: RpcEventHandler): () => void
}

export interface UseSessionListSubscriptionOptions {
  rpc: SessionListRpc
  isConnected: () => boolean
  refresh: () => void | Promise<void>
  scheduleRefresh: () => void
  warn?: (message: string, error?: unknown) => void
}

export function useSessionListSubscription(options: UseSessionListSubscriptionOptions) {
  let active = false
  let subscribed = false
  let connectionGeneration = 0
  let subscribeWork: Promise<void> | null = null
  let subscribeAttempt: symbol | null = null
  let removeListeners: Array<() => void> = []

  const warn = options.warn || ((message: string, error?: unknown) => {
    console.warn(message, error)
  })

  function invalidateConnection() {
    connectionGeneration += 1
    subscribed = false
    subscribeWork = null
    subscribeAttempt = null
  }

  function ensureSubscribed(): Promise<void> | null {
    if (!active || !options.isConnected() || subscribed) return null
    if (subscribeWork) return subscribeWork

    const generation = connectionGeneration
    const attempt = Symbol('session-list-subscription')
    subscribeAttempt = attempt
    subscribeWork = (async () => {
      try {
        await options.rpc.call('sessions.subscribe')
        if (!active || !options.isConnected() || generation !== connectionGeneration) return
        subscribed = true
        await options.refresh()
      } catch (error) {
        if (active && generation === connectionGeneration) {
          warn('Session list subscription failed', error)
        }
      } finally {
        if (subscribeAttempt === attempt) {
          subscribeWork = null
          subscribeAttempt = null
        }
      }
    })()
    return subscribeWork
  }

  function subscribe(): () => void {
    if (active) return cleanup
    active = true
    removeListeners = [
      options.rpc.on('sessions.changed', options.scheduleRefresh),
      options.rpc.on('_state', (state: ConnectionState) => {
        if (state === 'connected') {
          void ensureSubscribed()
          return
        }
        invalidateConnection()
      }),
    ]
    void ensureSubscribed()
    return cleanup
  }

  function cleanup() {
    if (!active) return
    const shouldUnsubscribe = subscribed || subscribeWork !== null
    active = false
    invalidateConnection()
    removeListeners.forEach(remove => remove())
    removeListeners = []

    if (shouldUnsubscribe && options.isConnected()) {
      void options.rpc.call('sessions.unsubscribe').catch(error => {
        warn('Session list unsubscribe failed', error)
      })
    }
  }

  return {
    subscribe,
    cleanup,
  }
}
