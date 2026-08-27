import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import {
  RpcClient,
  type RpcCallOptions,
  type RpcConnectionWaitOptions,
  type RpcEventHandler,
} from '@/lib/rpc'
import type { DesktopGatewayConnection } from '@/platform/types'
import { getPlatform } from '@/platform'

const WS_URL_KEY = 'opensquilla.wsUrl'
const WS_TOKEN_KEY = 'opensquilla.wsToken'
// Long-lived reconnect credential minted by a pairing claim; survives browser
// restarts, unlike the one-shot link token kept in sessionStorage.
const WS_DEVICE_TOKEN_KEY = 'opensquilla.deviceToken'
const CACHED_AUTH_KEY = 'opensquilla.cachedAuth'
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

function persistDeviceToken(token: string): void {
  try {
    localStorage.setItem(WS_DEVICE_TOKEN_KEY, token)
    // The one-shot pairing token is spent once the credential is issued.
    sessionStorage.removeItem(WS_TOKEN_KEY)
  } catch {}
}

function clearDeviceToken(): void {
  try { localStorage.removeItem(WS_DEVICE_TOKEN_KEY) } catch {}
}

/**
 * Read the one-shot pairing token from the URL fragment.
 *
 * The fragment is chosen deliberately: it is the only part of a URL a browser
 * never puts on the wire, so the secret cannot reach server access logs or
 * proxy logs on the initial navigation. A token in the query string would
 * already have been transmitted before any scrubbing could run, so one is
 * treated as unusable rather than silently accepted.
 */
function readLinkTokenFromFragment(url: URL): string {
  const raw = url.hash.startsWith('#') ? url.hash.slice(1) : url.hash
  if (!raw) return ''
  return (new URLSearchParams(raw).get('token') || '').trim()
}

function consumeLinkTokenFromUrl(): { url: string; token: string } | null {
  let url: URL
  try {
    url = new URL(window.location.href)
  } catch {
    return null
  }
  // A query-string token is already leaked by the time this runs. Strip it so
  // it is not persisted or reused, and refuse to authenticate with it.
  const leakedQueryToken = url.searchParams.has('token')
  const token = readLinkTokenFromFragment(url)
  if (!token) {
    if (leakedQueryToken) {
      try {
        url.searchParams.delete('token')
        window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
      } catch { /* restricted history */ }
    }
    return null
  }

  clearLinkTokenBrowserState()
  // Explicit re-pair: the scanned token supersedes any credential held here.
  clearDeviceToken()
  const rpcUrl = getDefaultRpcUrl()
  saveConnectionSettings(rpcUrl, token)

  try {
    url.searchParams.delete('token')
    // Drop the fragment too: leaving it in place would keep the spent secret
    // in browser history and in anything the user copies from the address bar.
    const cleaned = `${url.pathname}${url.search}`
    window.history.replaceState(null, '', cleaned)
  } catch {}

  return { url: rpcUrl, token }
}

function loadConnectionSettings(): { url: string; token: string } {
  let url = getDefaultRpcUrl()
  let token = ''
  try { url = localStorage.getItem(WS_URL_KEY) || url } catch {}
  try { token = sessionStorage.getItem(WS_TOKEN_KEY) || '' } catch {}
  if (!token) {
    try { token = localStorage.getItem(WS_DEVICE_TOKEN_KEY) || '' } catch {}
  }
  return { url, token }
}

function saveConnectionSettings(url: string, token: string): void {
  try { localStorage.setItem(WS_URL_KEY, url || getDefaultRpcUrl()) } catch {}
  try {
    if (token) sessionStorage.setItem(WS_TOKEN_KEY, token)
    else sessionStorage.removeItem(WS_TOKEN_KEY)
  } catch {}
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
  let desktopConnectionRevision = -1
  let desktopConnectionKey = ''
  let desktopAuthToken = ''

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
  const hasHostExecute = computed(() => {
    if (!isConnected.value) return false
    const principal = auth.value?.principal
    if (!principal || typeof principal !== 'object') return false
    const capabilities = (principal as Record<string, unknown>).capabilities
    return Array.isArray(capabilities) && capabilities.includes('host.execute')
  })
  const canManageProjectWorkspaces = computed(() =>
    (isLocalOwner.value || hasHostExecute.value)
    && supportsMethod('workspaces.list'))
  const canChooseProject = computed(() =>
    canManageProjectWorkspaces.value
    && supportsMethod('workspaces.open'))

  function clearConnectionIdentity(): void {
    policy.value = null
    auth.value = null
    methods.value = []
    events.value = []
    unavailableMethods.value = new Set()
  }

  function applyDesktopConnection(payload: DesktopGatewayConnection): void {
    if (
      !payload
      || payload.schemaVersion !== 1
      || !Number.isInteger(payload.revision)
      || payload.revision < desktopConnectionRevision
    ) return

    desktopConnectionRevision = payload.revision
    const nextUrl = typeof payload.wsUrl === 'string' ? payload.wsUrl.trim() : ''
    const nextInstance = typeof payload.instanceId === 'string' ? payload.instanceId : ''
    if (payload.status !== 'ready' || !nextUrl || !nextInstance) {
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
      if (client.value?.state !== 'disconnected') client.value?.disconnect()
      clearConnectionIdentity()
      return
    }

    const nextKey = `${nextInstance}\0${nextUrl}`
    if (nextKey === desktopConnectionKey) return
    const nextAuthToken = typeof payload.authToken === 'string'
      ? payload.authToken.trim()
      : ''
    desktopConnectionKey = nextKey
    if (nextAuthToken) {
      desktopAuthToken = nextAuthToken
      try { sessionStorage.setItem(WS_TOKEN_KEY, nextAuthToken) } catch {}
    }
    error.value = null
    if (client.value?.state !== 'disconnected') client.value?.disconnect()
    clearConnectionIdentity()
    client.value?.connect(nextUrl, nextAuthToken || loadConnectionSettings().token || undefined)
  }

  function init() {
    const rpc = new RpcClient()
    client.value = rpc

    rpc.on('_state', (s: 'disconnected' | 'connecting' | 'connected') => {
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
      policy.value = data.policy || null
      auth.value = data.auth || null
      methods.value = Array.isArray(data.features?.methods)
        ? data.features.methods.filter((method): method is string => typeof method === 'string')
        : []
      events.value = Array.isArray(data.features?.events)
        ? data.features.events.filter((event): event is string => typeof event === 'string')
        : []
      unavailableMethods.value = new Set()
      // A claimed pairing hands the phone a long-lived reconnect credential;
      // reconnects must switch to it (the pairing token is spent), and an
      // unauthenticated hello means a stored credential went stale.
      // The gateway nests this under the auth payload; a top-level read is
      // always undefined, which left the phone replaying the spent one-shot
      // pairing token after any reconnect.
      const issuedDeviceToken =
        typeof data.auth?.deviceToken === 'string' ? data.auth.deviceToken : ''
      if (issuedDeviceToken) {
        persistDeviceToken(issuedDeviceToken)
        client.value?.updateToken(issuedDeviceToken)
      } else {
        const principal = (data.auth?.principal ?? {}) as Record<string, unknown>
        if (principal.authenticated === false) {
          clearDeviceToken()
          client.value?.updateToken(null)
        }
      }
    })

    rpc.on('_gap', (detail: unknown) => {
      console.warn('[RPC] Sequence gap detected:', detail)
    })

    const gatewayPlatform = getPlatform().gateway
    if (
      typeof gatewayPlatform.getConnection === 'function'
      && typeof gatewayPlatform.onConnection === 'function'
    ) {
      gatewayPlatform.onConnection(applyDesktopConnection)
      void gatewayPlatform.getConnection().then(applyDesktopConnection).catch((reason) => {
        error.value = reason instanceof Error ? reason.message : String(reason)
      })
      return
    }

    // Browser Control UI keeps its same-origin bootstrap and optional link token.
    consumeLinkTokenFromUrl()
    const { url, token } = loadConnectionSettings()
    if (rpc.state === 'disconnected') {
      rpc.connect(url, token || undefined)
    }
  }

  async function connect(url: string, token?: string) {
    if (!client.value) throw new Error('RPC client not initialized')
    error.value = null
    saveConnectionSettings(url, token || '')
    client.value.connect(url, token)
  }

  function applyLinkTokenFromUrl(): boolean {
    const settings = consumeLinkTokenFromUrl()
    if (!settings) return false
    if (client.value) {
      client.value.disconnect()
      error.value = null
      policy.value = null
      auth.value = null
      methods.value = []
      events.value = []
      unavailableMethods.value = new Set()
      client.value.connect(settings.url, settings.token)
    }
    return true
  }

  function disconnect() {
    client.value?.disconnect()
    desktopConnectionKey = ''
    state.value = 'disconnected'
    clearConnectionIdentity()
  }

  function supportsMethod(method: string): boolean {
    return methods.value.includes(method) && !unavailableMethods.value.has(method)
  }

  function supportsEvent(event: string): boolean {
    return events.value.includes(event)
  }

  function markMethodUnavailable(method: string): void {
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

  function waitForConnection(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ): Promise<void> {
    if (!client.value) return Promise.reject(new Error('RPC client not initialized'))
    return client.value.waitForConnection(timeoutMs, signal, actions)
  }

  return {
    client,
    state,
    policy,
    auth,
    methods,
    events,
    error,
    isConnected,
    isConnecting,
    isLocalOwner,
    canManageProjectWorkspaces,
    canChooseProject,
    init,
    connect,
    applyLinkTokenFromUrl,
    disconnect,
    supportsMethod,
    supportsEvent,
    markMethodUnavailable,
    call,
    on,
    waitForConnection,
  }
})
