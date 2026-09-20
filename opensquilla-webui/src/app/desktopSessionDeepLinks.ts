import type { SessionDirectory } from '@/modules/sessionDirectory'
import type { PlatformWindowApi } from '@/platform/types'

interface GatewayContext {
  /** Empty until the initial Desktop Gateway connection is known. */
  endpoint: string
  epoch: number | null
  authenticated: boolean
}

interface DesktopSessionDeepLinkOptions {
  window: PlatformWindowApi
  directory: Pick<SessionDirectory, 'resolve'>
  gatewayContext: () => GatewayContext
  onGatewayContextChange: (callback: () => void) => () => void
  openSession: (key: string) => void
  unavailable: () => void
}

function validSessionKey(key: unknown): key is string {
  return typeof key === 'string'
    && key.length > 0
    && key.length <= 512
    && key === key.trim()
    && !/[\u0000-\u001f\u007f/\\]/.test(key)
}

/** Consume each desktop delivery once and resolve it before changing the route. */
export function bindDesktopSessionDeepLinks(options: DesktopSessionDeepLinkOptions): () => void {
  if (!options.window.onSessionDeepLink && !options.window.getPendingSessionDeepLink) return () => {}
  let disposed = false
  let generation = 0
  let pending: {
    key: string
    gateway: GatewayContext
    request: AbortController | null
  } | null = null

  function fail() {
    pending?.request?.abort()
    pending = null
    options.unavailable()
  }

  function gatewayChanged() {
    const target = pending
    if (disposed || !target) return
    const gateway = options.gatewayContext()
    if (
      (target.gateway.endpoint && gateway.endpoint && gateway.endpoint !== target.gateway.endpoint)
      || (target.gateway.epoch !== null && gateway.epoch !== target.gateway.epoch)
      || (target.gateway.authenticated && !gateway.authenticated)
    ) {
      fail()
      return
    }
    // Retain the acknowledged target in the renderer through arbitrarily slow
    // startup. Calling resolve before hello would consume its 10s RPC timeout
    // while the runtime is still booting, then lose the target permanently.
    if (gateway.epoch === null || !gateway.authenticated || target.request) return
    target.gateway = { ...gateway }
    const request = new AbortController()
    target.request = request
    void (async () => {
      try {
        const resolved = await options.directory.resolve({ key: target.key, signal: request.signal })
        if (disposed || pending !== target) return
        const currentGateway = options.gatewayContext()
        if (
          resolved.key !== target.key
          || !resolved.id
          || !currentGateway.authenticated
          || currentGateway.endpoint !== gateway.endpoint
          || currentGateway.epoch !== gateway.epoch
        ) {
          fail()
          return
        }
        pending = null
        options.openSession(target.key)
      } catch {
        if (!disposed && pending === target && !request.signal.aborted) fail()
      }
    })()
  }

  async function receive(eventKey?: string) {
    const current = ++generation
    pending?.request?.abort()
    pending = null
    const gateway = { ...options.gatewayContext() }
    try {
      // The IPC getter also acknowledges delivery in the main process. Do not
      // call receive again with its result: that would consume a newer target.
      const delivered = await options.window.getPendingSessionDeepLink?.()
      if (disposed || current !== generation) return
      const key = delivered ?? eventKey
      if (key === undefined || key === null) return
      if (!validSessionKey(key)) {
        options.unavailable()
        return
      }
      pending = { key, gateway, request: null }
      gatewayChanged()
    } catch {
      if (!disposed && current === generation) fail()
    }
  }

  const unsubscribe = options.window.onSessionDeepLink?.(key => { void receive(key) })
  const unsubscribeGateway = options.onGatewayContextChange(gatewayChanged)
  if (options.window.getPendingSessionDeepLink) void receive()
  return () => {
    disposed = true
    generation += 1
    pending?.request?.abort()
    pending = null
    unsubscribe?.()
    unsubscribeGateway()
  }
}
