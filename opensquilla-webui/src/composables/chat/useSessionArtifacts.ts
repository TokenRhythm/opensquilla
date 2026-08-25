import { computed, ref, watch, type Ref } from 'vue'

import {
  optionalSessionRpcCallOptions,
  waitForSessionRpcConnection,
} from '@/composables/chat/sessionBootstrapAdmission'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'
import type { ChatMessage } from '@/types/chat'
import type { ArtifactsListResponse, ArtifactPayload } from '@/types/rpc'

const ARTIFACTS_LIST_METHOD = 'artifacts.list'
const MAX_ARTIFACT_PAGE_LIMIT = 200

type ArtifactRpc = {
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  supportsMethod: (method: string) => boolean
  markMethodUnavailable: (method: string) => void
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    callOptions?: RpcCallOptions,
  ) => Promise<T>
}

export interface UseSessionArtifactsOptions {
  rpc: ArtifactRpc
  sessionKey: Ref<string>
  messages: Ref<ChatMessage[]>
  streamArtifacts: Ref<ArtifactPayload[]>
  pageLimit?: number
}

function artifactIdentity(artifact: ArtifactPayload): string {
  return String(artifact.id || artifact.download_url || artifact.name || '')
}

function mergeDefinedArtifactFields(
  current: ArtifactPayload,
  incoming: ArtifactPayload,
): ArtifactPayload {
  const merged: ArtifactPayload = { ...current }
  for (const [key, value] of Object.entries(incoming)) {
    if (value !== undefined) merged[key] = value
  }
  return merged
}

/**
 * Merge artifact sources without losing fields carried by another surface.
 *
 * Source order is significant: later sources update defined fields while the
 * first appearance keeps its position. The index therefore supplies stable
 * session order, history can add compatibility-only fields, and the live event
 * can update the newest wire metadata without duplicating the deliverable.
 */
export function mergeArtifactSources(
  ...sources: ReadonlyArray<ReadonlyArray<ArtifactPayload>>
): ArtifactPayload[] {
  const merged = new Map<string, ArtifactPayload>()
  for (const source of sources) {
    for (const artifact of source) {
      if (!artifact || typeof artifact !== 'object') continue
      const identity = artifactIdentity(artifact)
      if (!identity) continue
      const current = merged.get(identity)
      merged.set(
        identity,
        current ? mergeDefinedArtifactFields(current, artifact) : { ...artifact },
      )
    }
  }
  return [...merged.values()]
}

function responseArtifacts(response: ArtifactsListResponse): ArtifactPayload[] {
  return Array.isArray(response.artifacts)
    ? response.artifacts.filter(
      (artifact): artifact is ArtifactPayload =>
        !!artifact && typeof artifact === 'object' && !Array.isArray(artifact),
    )
    : []
}

function responseHasMore(response: ArtifactsListResponse): boolean {
  return Boolean(response.has_more ?? response.hasMore)
}

function responseOldestCursor(response: ArtifactsListResponse): string | null {
  const cursor = response.oldest_cursor ?? response.oldestCursor
  return typeof cursor === 'string' ? cursor : null
}

function isMethodNotFound(error: unknown): boolean {
  const code = (error as { code?: unknown } | null)?.code
  const message = error instanceof Error ? error.message : String(error)
  return code === 'METHOD_NOT_FOUND' || /method not found/i.test(message)
}

function isRpcTimeout(error: unknown): boolean {
  return (error as { code?: unknown } | null)?.code === 'RPC_TIMEOUT'
}

function normalizedPageLimit(value: number | undefined): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return MAX_ARTIFACT_PAGE_LIMIT
  return Math.min(MAX_ARTIFACT_PAGE_LIMIT, Math.max(1, Math.floor(value)))
}

export function useSessionArtifacts(options: UseSessionArtifactsOptions) {
  const indexedArtifacts = ref<ArtifactPayload[]>([])
  const loading = ref(false)
  const indexAvailable = ref(false)
  let indexedSessionKey = ''
  let requestSequence = 0
  let activeRequestController: AbortController | null = null
  let suppressNextReconnectLoad = false
  let streamRefreshPending = false

  // Artifact events are intentionally also kept in the transient turn stream
  // for mixed-version compatibility. A publication, however, must survive the
  // next turn's stream reset. Refresh the durable index whenever a new live
  // artifact identity appears so the event becomes a stable session resource
  // before that transient source is cleared.
  const stopStreamArtifactRefresh = watch(
    () => options.streamArtifacts.value.map(artifactIdentity).filter(Boolean).join('\0'),
    (current, previous) => {
      if (!current || current === previous || !String(options.sessionKey.value || '').trim()) {
        return
      }
      if (loading.value) {
        streamRefreshPending = true
        return
      }
      void load()
    },
  )

  const historyArtifacts = computed<ArtifactPayload[]>(() =>
    options.messages.value.flatMap(message => message.artifacts || []),
  )

  // Always keep all three sources in the union. This preserves the old-gateway
  // history fallback, keeps a just-published live artifact visible while list
  // pagination is in flight, and lets the durable index fill compacted history.
  const artifacts = computed<ArtifactPayload[]>(() => mergeArtifactSources(
    indexedArtifacts.value,
    historyArtifacts.value,
    options.streamArtifacts.value,
  ))

  function cancelActiveRequest() {
    const controller = activeRequestController
    activeRequestController = null
    controller?.abort()
  }

  function reset() {
    // Retire the generation before aborting. RpcClient rejects an aborted call
    // asynchronously, and that stale catch must not disable the method or
    // mutate state owned by a newer Session.
    requestSequence += 1
    cancelActiveRequest()
    indexedSessionKey = ''
    indexedArtifacts.value = []
    loading.value = false
    indexAvailable.value = false
    suppressNextReconnectLoad = false
    streamRefreshPending = false
  }

  async function load(): Promise<boolean> {
    // An explicit load (including the one started for a new Session) is never
    // suppressed by a prior page timeout. Only the reconnect-specific entry
    // point below consumes that one-shot guard.
    suppressNextReconnectLoad = false
    const sessionKey = String(options.sessionKey.value || '').trim()
    const requestId = ++requestSequence
    // A reconnect refresh or Session switch supersedes the complete prior page
    // walk. The shared optional-RPC policy recycles a socket whose serialized
    // request is stuck, so critical chat traffic cannot remain queued behind it.
    cancelActiveRequest()
    if (!sessionKey) {
      indexedSessionKey = ''
      indexedArtifacts.value = []
      loading.value = false
      indexAvailable.value = false
      return false
    }
    const controller = new AbortController()
    activeRequestController = controller
    const callOptions: RpcCallOptions = {
      ...optionalSessionRpcCallOptions,
      signal: controller.signal,
    }

    const crossedSession = indexedSessionKey !== sessionKey
    if (crossedSession) {
      indexedSessionKey = sessionKey
      indexedArtifacts.value = []
      indexAvailable.value = false
    }
    loading.value = true

    const isCurrentRequest = () =>
      requestId === requestSequence && sessionKey === options.sessionKey.value

    try {
      // Hello owns the method capability list. Wait for it before checking or a
      // connecting client would look exactly like an older unsupported gateway.
      await waitForSessionRpcConnection(options.rpc, callOptions)
      if (!isCurrentRequest()) return false
      if (!options.rpc.supportsMethod(ARTIFACTS_LIST_METHOD)) {
        // A reconnect can temporarily land on an older Gateway. Retain a
        // successful index for this same Session; a true Session switch already
        // cleared it above before any asynchronous work began.
        indexAvailable.value = false
        return false
      }

      const pageLimit = normalizedPageLimit(options.pageLimit)
      const visitedCursors = new Set<string>()
      let before: string | null = null
      let collected: ArtifactPayload[] = []
      let pageRequestInFlight = false

      while (true) {
        const params: Record<string, unknown> = { sessionKey, limit: pageLimit }
        if (before !== null) params.before = before
        pageRequestInFlight = true
        let response: ArtifactsListResponse
        try {
          response = await options.rpc.call<ArtifactsListResponse>(
            ARTIFACTS_LIST_METHOD,
            params,
            callOptions,
          )
        } catch (error) {
          // RpcClient recycles the shared socket when this bounded page call
          // times out. Suppress the reconnect that this request caused, or a
          // persistently slow artifact directory would create an automatic
          // timeout/reconnect/list loop. Connection-wait timeouts never pass
          // through this branch.
          if (isCurrentRequest() && pageRequestInFlight && isRpcTimeout(error)) {
            suppressNextReconnectLoad = true
          }
          throw error
        } finally {
          pageRequestInFlight = false
        }
        if (!isCurrentRequest()) return false

        // The endpoint returns the latest page first and each page oldest to
        // newest. Older pages are prepended while duplicate boundary entries
        // still merge their fields deterministically.
        const pageArtifacts = responseArtifacts(response)
        collected = mergeArtifactSources(pageArtifacts, collected)
        if (!responseHasMore(response)) break

        const nextCursor = responseOldestCursor(response)
        if (nextCursor === null || pageArtifacts.length === 0) {
          throw new Error('Artifact pagination did not provide an advancing cursor')
        }
        if (visitedCursors.has(nextCursor)) {
          throw new Error('Artifact pagination cursor did not advance')
        }
        visitedCursors.add(nextCursor)
        before = nextCursor
      }

      indexedArtifacts.value = collected
      indexAvailable.value = true
      return true
    } catch (error) {
      if (!isCurrentRequest()) return false
      if (isMethodNotFound(error)) {
        options.rpc.markMethodUnavailable(ARTIFACTS_LIST_METHOD)
      }
      // A missing or transiently failed index must never blank the legacy
      // history/live sources. Keep a previous same-session index on refresh
      // errors; crossed Sessions were already cleared synchronously above.
      indexAvailable.value = false
      return false
    } finally {
      if (isCurrentRequest()) {
        if (activeRequestController === controller) activeRequestController = null
        loading.value = false
        if (streamRefreshPending) {
          streamRefreshPending = false
          queueMicrotask(() => void load())
        }
      }
    }
  }

  function loadAfterReconnect(): Promise<boolean> {
    if (suppressNextReconnectLoad) {
      suppressNextReconnectLoad = false
      return Promise.resolve(false)
    }
    return load()
  }

  function cleanup() {
    stopStreamArtifactRefresh()
    reset()
  }

  return {
    artifacts,
    indexedArtifacts,
    indexAvailable,
    loading,
    load,
    loadAfterReconnect,
    reset,
    cleanup,
  }
}
