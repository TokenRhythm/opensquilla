import { ref, computed, onScopeDispose } from 'vue'
import { defineStore } from 'pinia'
import {
  RpcClient,
  type RpcCallOptions,
  type RpcConnectionWaitOptions,
  type RpcEventHandler,
  type RpcLifecycle,
  type RpcConsumptionHandler,
} from '@/lib/rpc'
import type { DesktopGatewayConnection } from '@/platform/types'
import { getPlatform } from '@/platform'
import { recordRpcTransportDiag } from '@/utils/chat/sessionNavigationDiag'

const WS_URL_KEY = 'opensquilla.wsUrl'
const WS_TOKEN_KEY = 'opensquilla.wsToken'
const CACHED_AUTH_KEY = 'opensquilla.cachedAuth'
const DELIVERY_SALT_KEY = 'opensquilla.deliverySalt.v1'
const CHAT_DRAFT_PREFIX = 'opensquilla.chat.draft:'

function getDefaultRpcUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}

function clearStoragePrefix(storage: Storage, prefix: string): void {
  try {
    for (const key of Object.keys(storage)) {
      if (key.startsWith(prefix)) storage.removeItem(key)
    }
  } catch {}
}

function clearLinkTokenBrowserState(): void {
  try {
    localStorage.removeItem(WS_URL_KEY)
    clearStoragePrefix(localStorage, CHAT_DRAFT_PREFIX)
  } catch {}
  try {
    sessionStorage.removeItem(WS_TOKEN_KEY)
    sessionStorage.removeItem(CACHED_AUTH_KEY)
  } catch {}
}

function consumeLinkTokenFromUrl(): { url: string; token: string } | null {
  let url: URL
  try {
    url = new URL(window.location.href)
  } catch {
    return null
  }
  const token = (url.searchParams.get('token') || '').trim()
  if (!token) return null

  clearLinkTokenBrowserState()
  const rpcUrl = getDefaultRpcUrl()
  saveConnectionSettings(rpcUrl, token)

  try {
    url.searchParams.delete('token')
    const cleaned = `${url.pathname}${url.search}${url.hash}`
    window.history.replaceState(null, '', cleaned)
  } catch {}

  return { url: rpcUrl, token }
}

function loadConnectionSettings(): { url: string; token: string } {
  let url = getDefaultRpcUrl()
  let token = ''
  try { url = localStorage.getItem(WS_URL_KEY) || url } catch {}
  try { token = sessionStorage.getItem(WS_TOKEN_KEY) || '' } catch {}
  return { url, token }
}

function saveConnectionSettings(url: string, token: string): void {
  try { localStorage.setItem(WS_URL_KEY, url || getDefaultRpcUrl()) } catch {}
  try {
    if (token) sessionStorage.setItem(WS_TOKEN_KEY, token)
    else sessionStorage.removeItem(WS_TOKEN_KEY)
  } catch {}
}

async function deliverySalt(): Promise<string | null> {
  const existing = localStorage.getItem(DELIVERY_SALT_KEY)
  if (existing && /^[0-9a-f]{32}$/.test(existing)) return existing
  // Queue WAL is origin-shared, so its salt must be too. Only create it under
  // the same origin lock: a read/set race would strand the losing tab's rows.
  // Older browsers can reuse a salt, but cannot safely create one without locks.
  if (!navigator.locks?.request) return null
  return navigator.locks.request(DELIVERY_SALT_KEY, () => {
    const current = localStorage.getItem(DELIVERY_SALT_KEY)
    if (current && /^[0-9a-f]{32}$/.test(current)) return current
    const salt = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
    localStorage.setItem(DELIVERY_SALT_KEY, salt)
    return salt
  })
}

export const useRpcStore = defineStore('rpc', () => {
  const client = ref<RpcClient | null>(null)
  const state = ref<'disconnected' | 'connecting' | 'connected'>('disconnected')
  const policy = ref<Record<string, unknown> | null>(null)
  const auth = ref<Record<string, unknown> | null>(null)
  const methods = ref<string[]>([])
  const events = ref<string[]>([])
  const unavailableMethods = ref<Set<string>>(new Set())
  const error = ref<string | null>(null)
  const lifecycle = ref<RpcLifecycle>('stopped')
  const health = ref<'healthy' | 'suspect'>('healthy')
  // RpcClient is stored as a class instance and several callbacks retain the
  // raw object, so its private generation mutations are not Vue-reactive.
  // Mirror the value explicitly at every transport/state boundary.
  const connectionGeneration = ref(0)
  const deliveryContext = ref<{ targetId: string; principal: unknown } | null>(null)
  // Tabs in this browser profile may recover their shared queue only after
  // both the salted target hash and Hello principal agree. Only a random salt
  // persists here; raw target credentials never enter the queue identity.
  let deliveryIntent = 0
  let deliveryTargetId = ''
  let deliveryProof: Record<string, unknown> | null = null
  let browserConnectionUrl = ''
  let browserAuthToken = ''
  let desktopConnectionRevision = -1
  let desktopConnectionKey = ''
  let desktopAuthToken = ''
  let connectionDesired = true
  let authRefreshAttempted = false
  let descriptorTimer: ReturnType<typeof setTimeout> | null = null
  let descriptorFetch: {
    revision: number
    manual: boolean
    retry: boolean
    promise: Promise<void>
  } | null = null
  let descriptorAttempt = 0
  let descriptorRequestRevision = 0
  const connectionSubscriptions: Array<() => void> = []

  onScopeDispose(() => {
    connectionDesired = false
    deliveryProof = null
    deliveryContext.value = null
    cancelDescriptorRecovery()
    for (const unsubscribe of connectionSubscriptions.splice(0)) unsubscribe()
    client.value?.disconnect()
  })

  function cancelDescriptorRecovery(): void {
    descriptorRequestRevision += 1
    if (descriptorTimer !== null) clearTimeout(descriptorTimer)
    descriptorTimer = null
  }

  function refreshDesktopConnection(retry = true, manual = false): Promise<void> {
    const getConnection = getPlatform().gateway.getConnection
    if (!connectionDesired || !getConnection) return Promise.resolve()
    if (descriptorFetch?.revision === descriptorRequestRevision) {
      descriptorFetch.manual ||= manual
      descriptorFetch.retry ||= retry
      return descriptorFetch.promise
    }
    const revision = descriptorRequestRevision
    const request = { revision, manual, retry, promise: Promise.resolve() }
    let timeout: ReturnType<typeof setTimeout> | undefined
    request.promise = Promise.race([
      Promise.resolve().then(() => getConnection()),
      new Promise<never>((_, reject) => {
        timeout = setTimeout(() => reject(new Error('Gateway descriptor unavailable')), 8_000)
      }),
    ]).then(payload => {
      if (revision !== descriptorRequestRevision || !connectionDesired) return
      descriptorAttempt = 0
      applyDesktopConnection(payload, request.manual)
    }).catch(() => {
      if (revision !== descriptorRequestRevision || !connectionDesired) return
      error.value = 'Gateway connection information is temporarily unavailable'
      if (request.retry && descriptorTimer === null) {
        const cap = Math.min(15_000, 500 * 2 ** Math.min(descriptorAttempt++, 10))
        descriptorTimer = setTimeout(() => {
          descriptorTimer = null
          refreshDesktopConnection()
        }, Math.max(250, Math.floor(cap * (0.5 + Math.random() * 0.5))))
      }
    }).finally(() => {
      if (timeout !== undefined) clearTimeout(timeout)
      if (descriptorFetch === request) descriptorFetch = null
    })
    descriptorFetch = request
    return request.promise
  }

  const isConnected = computed(() => state.value === 'connected')
  const isConnecting = computed(() => state.value === 'connecting')
  const isLocalOwner = computed(() => {
    if (!isConnected.value) return false
    const principal = auth.value?.principal
    return Boolean(
      principal
      && typeof principal === 'object'
      && (principal as Record<string, unknown>).isOwner === true,
    )
  })
  const canManageProjectWorkspaces = computed(() =>
    isLocalOwner.value
    && hasRpcMethod('workspaces.list'))
  const canChooseProject = computed(() =>
    canManageProjectWorkspaces.value
    && hasRpcMethod('workspaces.open'))

  function clearConnectionIdentity(): void {
    policy.value = null
    auth.value = null
    methods.value = []
    events.value = []
    unavailableMethods.value = new Set()
  }

  function beginDeliveryIntent(target: string | null): void {
    const intent = ++deliveryIntent
    deliveryTargetId = ''
    deliveryProof = null
    deliveryContext.value = null
    if (!target) return
    // Hashing runs alongside transport startup; it never delays the connection.
    void (async () => {
      try {
        const salt = await deliverySalt()
        if (!salt || intent !== deliveryIntent || !connectionDesired) return
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([salt, target])))
        if (intent !== deliveryIntent || !connectionDesired) return
        deliveryTargetId = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
        proveDeliveryContext(deliveryProof)
      } catch {
        // Storage/crypto restrictions disable offline delivery, not startup.
      }
    })()
  }

  function proveDeliveryContext(helloAuth: Record<string, unknown> | null): void {
    deliveryProof = connectionDesired ? helloAuth : null
    deliveryContext.value = deliveryTargetId && deliveryProof
      ? { targetId: deliveryTargetId, principal: deliveryProof.principal }
      : null
  }

  function connectBrowser(url: string, token?: string): void {
    const sameTarget = browserConnectionUrl === url && browserAuthToken === (token || '')
    const provenAuth = sameTarget && state.value === 'connected' ? auth.value : null
    beginDeliveryIntent(JSON.stringify(['browser', url, token || '']))
    browserConnectionUrl = url
    browserAuthToken = token || ''
    if (!sameTarget) clearConnectionIdentity()
    client.value?.connect(url, token)
    // An explicit Connect can reuse a healthy socket and its Hello. A changed
    // target must wait for its own proof before queued delivery is eligible.
    if (provenAuth && state.value === 'connected') proveDeliveryContext(provenAuth)
  }

  function applyDesktopConnection(payload: DesktopGatewayConnection, manual = false): void {
    if (
      !connectionDesired
      || !payload
      || payload.schemaVersion !== 1
      || !Number.isInteger(payload.revision)
      || payload.revision < desktopConnectionRevision
    ) {
      if (manual && connectionDesired && (!payload || payload.schemaVersion !== 1)) {
        error.value = 'Gateway connection information is temporarily unavailable'
      }
      return
    }

    desktopConnectionRevision = payload.revision
    const nextUrl = typeof payload.wsUrl === 'string' ? payload.wsUrl.trim() : ''
    const nextInstance = typeof payload.instanceId === 'string' ? payload.instanceId : ''
    if (payload.status !== 'ready' || !nextUrl || !nextInstance) {
      deliveryProof = null
      deliveryContext.value = null
      if (manual) {
        error.value = payload.error || 'Gateway is not ready to connect'
        return
      }
      desktopConnectionKey = ''
      if (desktopAuthToken) {
        try {
          if (sessionStorage.getItem(WS_TOKEN_KEY) === desktopAuthToken) {
            sessionStorage.removeItem(WS_TOKEN_KEY)
          }
        } catch {}
        desktopAuthToken = ''
      }
      error.value = payload.error || null
      if (client.value?.lifecycle !== 'stopped') client.value?.disconnect()
      clearConnectionIdentity()
      return
    }

    const nextAuthToken = typeof payload.authToken === 'string'
      ? payload.authToken.trim()
      : ''
    const nextKey = `${payload.profileFingerprint}\0${nextInstance}\0${nextUrl}`
    const explicitRestart = manual && (
      client.value?.lifecycle === 'stopped' || client.value?.lifecycle === 'blocked'
    )
    if (nextKey === desktopConnectionKey && nextAuthToken === desktopAuthToken && !explicitRestart) {
      if (client.value?.lifecycle !== 'blocked') error.value = null
      client.value?.ensureConnected()
      if (manual && state.value === 'connected' && client.value?.lifecycle !== 'blocked') {
        proveDeliveryContext(auth.value)
      }
      return
    }
    beginDeliveryIntent(JSON.stringify(['desktop', nextKey, nextAuthToken]))
    authRefreshAttempted = false
    desktopConnectionKey = nextKey
    if (desktopAuthToken && !nextAuthToken) {
      try {
        if (sessionStorage.getItem(WS_TOKEN_KEY) === desktopAuthToken) sessionStorage.removeItem(WS_TOKEN_KEY)
      } catch {}
    }
    desktopAuthToken = nextAuthToken
    if (nextAuthToken) {
      try { sessionStorage.setItem(WS_TOKEN_KEY, nextAuthToken) } catch {}
    }
    error.value = null
    if (client.value?.state !== 'disconnected') client.value?.disconnect()
    clearConnectionIdentity()
    client.value?.connect(nextUrl, nextAuthToken || undefined, {
      key: nextKey,
      authentication: nextAuthToken ? 'owner' : 'guest-allowed',
    })
  }

  function init() {
    if (client.value) return
    connectionDesired = true
    const rpc = new RpcClient()
    client.value = rpc

    rpc.on('_status', (status: { lifecycle: RpcLifecycle; health: 'healthy' | 'suspect'; reason: string | null }) => {
      lifecycle.value = status.lifecycle
      health.value = status.health
      if (status.lifecycle === 'blocked') error.value = status.reason
      if (status.lifecycle === 'blocked' || status.lifecycle === 'stopped') {
        deliveryProof = null
        deliveryContext.value = null
      }
    })
    rpc.on('_blocked', () => {
      if (authRefreshAttempted || !connectionDesired) return
      authRefreshAttempted = true
      refreshDesktopConnection(false)
    })

    rpc.on('_state', (s: 'disconnected' | 'connecting' | 'connected') => {
      connectionGeneration.value = rpc.connectionGeneration
      state.value = s
      if (s !== 'connected') {
        clearConnectionIdentity()
      }
    })

    rpc.on('_hello', (data: {
      policy?: Record<string, unknown>
      auth?: Record<string, unknown>
      features?: { methods?: unknown; events?: unknown }
    }) => {
      error.value = null
      policy.value = data.policy || null
      auth.value = data.auth || null
      proveDeliveryContext(auth.value)
      methods.value = Array.isArray(data.features?.methods)
        ? data.features.methods.filter((method): method is string => typeof method === 'string')
        : []
      events.value = Array.isArray(data.features?.events)
        ? data.features.events.filter((event): event is string => typeof event === 'string')
        : []
      unavailableMethods.value = new Set()
    })

    rpc.on('_gap', (detail: unknown) => {
      console.warn('[RPC] Sequence gap detected:', detail)
    })

    rpc.on('_transport', (detail: unknown) => {
      connectionGeneration.value = rpc.connectionGeneration
      recordRpcTransportDiag(detail)
    })

    const gatewayPlatform = getPlatform().gateway
    if (gatewayPlatform.onResume) {
      connectionSubscriptions.push(gatewayPlatform.onResume(notifyResume))
    }
    if (
      typeof gatewayPlatform.getConnection === 'function'
      && typeof gatewayPlatform.onConnection === 'function'
    ) {
      connectionSubscriptions.push(gatewayPlatform.onConnection(payload => applyDesktopConnection(payload)))
      refreshDesktopConnection()
      return
    }

    // Browser Control UI keeps its same-origin bootstrap and optional link token.
    consumeLinkTokenFromUrl()
    const { url, token } = loadConnectionSettings()
    if (rpc.state === 'disconnected') {
      connectBrowser(url, token || undefined)
    }
  }

  async function connect(url: string, token?: string) {
    if (!client.value) throw new Error('RPC client not initialized')
    error.value = null
    connectionDesired = true
    if (getPlatform().id === 'desktop') {
      const provenAuth = state.value === 'connected' && client.value.lifecycle !== 'blocked'
        ? auth.value
        : null
      beginDeliveryIntent(desktopConnectionKey
        ? JSON.stringify(['desktop', desktopConnectionKey, desktopAuthToken])
        : null)
      // Refresh does not replace a healthy Desktop socket until its supervisor
      // supplies another descriptor. Its existing Hello remains proof of the
      // current target even if that read fails.
      if (provenAuth) proveDeliveryContext(provenAuth)
      // The renderer origin and its form token are not the Desktop runtime's
      // endpoint or credentials. Keep a working socket until its owner replies.
      if (!getPlatform().gateway.getConnection) {
        error.value = 'Gateway connection information is temporarily unavailable'
        return
      }
      const retry = descriptorTimer !== null
      if (descriptorTimer !== null) clearTimeout(descriptorTimer)
      descriptorTimer = null
      await refreshDesktopConnection(retry, true)
      return
    }
    cancelDescriptorRecovery()
    saveConnectionSettings(url, token || '')
    connectBrowser(url, token)
  }

  function applyLinkTokenFromUrl(): boolean {
    const settings = consumeLinkTokenFromUrl()
    if (!settings) return false
    if (client.value) {
      connectionDesired = true
      deliveryContext.value = null
      client.value.disconnect()
      error.value = null
      policy.value = null
      auth.value = null
      methods.value = []
      events.value = []
      unavailableMethods.value = new Set()
      connectBrowser(settings.url, settings.token)
    }
    return true
  }

  function disconnect() {
    connectionDesired = false
    beginDeliveryIntent(null)
    cancelDescriptorRecovery()
    client.value?.disconnect()
    desktopConnectionKey = ''
    state.value = 'disconnected'
    clearConnectionIdentity()
  }

  function notifyResume(): void {
    if (!connectionDesired) return
    client.value?.notifyResume()
    refreshDesktopConnection()
  }

  function onGap(handler: (detail: unknown) => Promise<boolean>): () => void {
    return client.value?.onGap(handler) || (() => {})
  }

  function onConsumedEvent(event: string, handler: RpcConsumptionHandler): () => void {
    return client.value?.onConsumedEvent(event, handler) || (() => {})
  }

  function enableConsumptionFlow(): void { client.value?.enableConsumptionFlow() }

  function consumeEvent(event: string, payload: unknown, meta: Record<string, unknown>) {
    if (!client.value) return Promise.reject(new Error('RPC client not initialized'))
    return client.value.consumeEvent(event, payload, meta)
  }

  function recoverGap(detail: unknown): Promise<boolean> {
    return client.value?.recoverGap(detail) || Promise.resolve(false)
  }

  function hasRpcMethod(method: string): boolean {
    return methods.value.includes(method) && !unavailableMethods.value.has(method)
  }

  function hasRpcEvent(event: string): boolean {
    return events.value.includes(event)
  }

  function rememberUnsupportedMethod(method: string): void {
    if (!method) return
    unavailableMethods.value = new Set([...unavailableMethods.value, method])
  }

  async function call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T> {
    if (!client.value) throw new Error('RPC client not initialized')
    if (state.value !== 'connected') {
      throw new Error(`Cannot call ${method}: not connected (state: ${state.value})`)
    }
    return (
      options
        ? client.value.call(method, params, options)
        : client.value.call(method, params)
    ) as Promise<T>
  }

  function on(event: string, handler: RpcEventHandler): () => void {
    if (!client.value) {
      console.warn(`[RPC] No client for event subscription: ${event}`)
      return () => {}
    }
    return client.value.on(event, handler)
  }

  function ready(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ): Promise<void> {
    if (!client.value) return Promise.reject(new Error('RPC client not initialized'))
    return client.value.ready(timeoutMs, signal, actions)
  }

  function recoverConnectionGeneration(
    expectedGeneration: number,
    reason: string,
  ): boolean {
    return client.value?.recoverConnectionGeneration(expectedGeneration, reason) ?? false
  }

  return {
    client,
    state,
    policy,
    auth,
    methods,
    events,
    error,
    lifecycle,
    health,
    connectionGeneration,
    deliveryContext,
    isConnected,
    isConnecting,
    isLocalOwner,
    canManageProjectWorkspaces,
    canChooseProject,
    init,
    connect,
    applyLinkTokenFromUrl,
    disconnect,
    notifyResume,
    onGap,
    onConsumedEvent,
    enableConsumptionFlow,
    consumeEvent,
    recoverGap,
    hasRpcMethod,
    hasRpcEvent,
    rememberUnsupportedMethod,
    call,
    on,
    ready,
    recoverConnectionGeneration,
  }
})
