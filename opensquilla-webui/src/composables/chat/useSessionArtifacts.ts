import { computed, ref, watch, type Ref } from 'vue'

import type { ArtifactCatalog } from '@/modules/artifactWorkbench'
import type { ChatMessage } from '@/types/chat'
import type { ArtifactPayload } from '@/types/artifacts'

const MAX_ARTIFACT_PAGE_LIMIT = 200

export interface UseSessionArtifactsOptions {
  catalog: ArtifactCatalog
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

function isRpcTimeout(error: unknown): boolean {
  return (error as { code?: unknown } | null)?.code === 'RPC_TIMEOUT'
}

function isArtifactListTimeout(error: unknown): boolean {
  return isRpcTimeout(error)
    && (error as { artifactCatalogPhase?: unknown } | null)?.artifactCatalogPhase !== 'connect'
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
      const collected = await options.catalog.listSession(sessionKey, {
        limit: normalizedPageLimit(options.pageLimit),
        signal: controller.signal,
      })
      if (!isCurrentRequest()) return false
      if (collected === null) {
        indexAvailable.value = false
        return false
      }
      indexedArtifacts.value = collected
      indexAvailable.value = true
      return true
    } catch (error) {
      if (!isCurrentRequest()) return false
      if (isArtifactListTimeout(error)) suppressNextReconnectLoad = true
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
