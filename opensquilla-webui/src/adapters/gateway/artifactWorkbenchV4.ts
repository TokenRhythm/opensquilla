import type {
  ArtifactCatalog,
  ArtifactDocumentChange,
  ArtifactWorkbench,
  ArtifactWorkbenchSubscription,
} from '@/modules/artifactWorkbench'
import { ArtifactCatalogError } from '@/modules/artifactWorkbench'
import {
  readTransportFailure,
} from './privateTransports'
import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  ARTIFACTS_LIST_METHOD,
} from '@/contracts/generated/v4/artifactsList'
import { validateArtifactsListResult } from '@/contracts/generated/v4/artifactsListValidators.mjs'
import type { ArtifactPayload } from '@/types/artifacts'
import { createV4ArtifactDocuments } from './artifactDocumentsV4'
import { createV4ArtifactPromptAnnotations } from './artifactPromptAnnotationsV4'
import { createV4WorkbenchResources } from './workbenchResourcesV4'
import { createV4ArtifactContentAccess } from './artifactAccessV4'
import { createV4AttachmentContentAccess } from './attachmentAccessV4'
import { createV4ArtifactPreviews } from './artifactPreviewsV4'
import { documentChangeEventContract } from './artifactWorkbenchContracts'

type ArtifactWorkbenchHttpTransport = Parameters<typeof createV4ArtifactContentAccess>[0]
  & Parameters<typeof createV4AttachmentContentAccess>[0]
  & Parameters<typeof createV4ArtifactPreviews>[0]

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: Pick<
    RpcCallOptions,
    'signal' | 'timeoutMs' | 'timeoutAction' | 'abortAction'
  >): Promise<void>
  supports(method: string): boolean
  markUnsupported(method: string): void
}

interface TransportSubscription {
  close(): void
}

interface EventTransport {
  subscribe(event: string, handler: (payload: unknown) => void): TransportSubscription
}

const MAX_ARTIFACT_PAGE_LIMIT = 200

interface ArtifactPageShape {
  artifacts?: unknown
  has_more?: boolean
  hasMore?: boolean
  oldest_cursor?: string | null
  oldestCursor?: string | null
}

function artifactPageItems(response: ArtifactPageShape): ArtifactPayload[] {
  return Array.isArray(response.artifacts)
    ? response.artifacts.filter(
      (artifact): artifact is ArtifactPayload => (
        !!artifact && typeof artifact === 'object' && !Array.isArray(artifact)
      ),
    )
    : []
}

function artifactIdentity(artifact: ArtifactPayload): string {
  return String(artifact.id || artifact.download_url || artifact.name || '')
}

function mergeArtifacts(
  older: readonly ArtifactPayload[],
  newer: readonly ArtifactPayload[],
): ArtifactPayload[] {
  const merged = new Map<string, ArtifactPayload>()
  for (const artifact of [...older, ...newer]) {
    const identity = artifactIdentity(artifact)
    if (!identity) continue
    const current = merged.get(identity)
    merged.set(identity, current ? { ...current, ...artifact } : { ...artifact })
  }
  return [...merged.values()]
}

function methodNotFound(error: unknown): boolean {
  const code = (error as { code?: unknown } | null)?.code
  return code === 'METHOD_NOT_FOUND'
    || /method not found/i.test(error instanceof Error ? error.message : String(error || ''))
}

function catalogError(
  error: unknown,
  phase: 'connect' | 'list',
): ArtifactCatalogError {
  if (error instanceof ArtifactCatalogError) return error
  const failure = readTransportFailure(error)
  const code = failure.code?.toUpperCase()
  const kind = code === 'RPC_ABORTED' || (error instanceof Error && error.name === 'AbortError')
    ? 'aborted'
    : code === 'RPC_TIMEOUT'
      ? 'timeout'
      : 'unavailable'
  return new ArtifactCatalogError(kind, phase, failure.message, error)
}

function createV4ArtifactCatalog(rpc: RpcTransport): ArtifactCatalog {
  return {
    async listSession(sessionKey, options = {}) {
      try {
        await rpc.ready({
          signal: options.signal,
          timeoutMs: 10_000,
          timeoutAction: 'reject',
          abortAction: 'reject',
        })
      } catch (error) {
        throw catalogError(error, 'connect')
      }
      if (!rpc.supports(ARTIFACTS_LIST_METHOD)) return null
      const limit = typeof options.limit === 'number' && Number.isFinite(options.limit)
        ? Math.min(MAX_ARTIFACT_PAGE_LIMIT, Math.max(1, Math.floor(options.limit)))
        : MAX_ARTIFACT_PAGE_LIMIT
      const visitedCursors = new Set<string>()
      let before: string | null = null
      let collected: ArtifactPayload[] = []
      try {
        for (;;) {
          const rawResponse = await rpc.request<unknown>(
            ARTIFACTS_LIST_METHOD,
            {
              sessionKey,
              limit,
              ...(before === null ? {} : { before }),
            },
            {
              signal: options.signal,
              timeoutMs: 10_000,
              timeoutAction: 'reconnect',
              abortAction: 'reject',
            },
          )
          const canonical = validateArtifactsListResult(rawResponse)
          const response = rawResponse as ArtifactPageShape
          if (
            !Array.isArray(response.artifacts)
            || (!canonical && typeof response.hasMore !== 'boolean')
            || typeof (response.has_more ?? response.hasMore) !== 'boolean'
          ) {
            throw new ArtifactCatalogError(
              'invalid',
              'list',
              'Artifact catalog response violated its v4 contract',
            )
          }
          const page = artifactPageItems(response)
          collected = mergeArtifacts(page, collected)
          if (!Boolean(response.has_more ?? response.hasMore)) break
          const cursor: unknown = response.oldest_cursor ?? response.oldestCursor
          if (
            typeof cursor !== 'string'
            || page.length === 0
            || visitedCursors.has(cursor)
          ) {
            throw new ArtifactCatalogError(
              'invalid',
              'list',
              'Artifact pagination did not provide an advancing cursor',
            )
          }
          visitedCursors.add(cursor)
          before = cursor
        }
        return collected
      } catch (error) {
        if (!methodNotFound(error)) {
          throw catalogError(error, 'list')
        }
        rpc.markUnsupported(ARTIFACTS_LIST_METHOD)
        return null
      }
    },
  }
}

function documentChange(value: unknown): (ArtifactDocumentChange & { sequence: number }) | null {
  if (!documentChangeEventContract.validatePayload(value)) return null
  const raw = value as Record<string, unknown>
  const documentId = typeof raw.documentId === 'string'
    ? raw.documentId.trim()
    : typeof raw.document_id === 'string' ? raw.document_id.trim() : ''
  const sequence = Number(raw.artifactEventSeq ?? raw.artifact_event_seq)
  return documentId && Number.isSafeInteger(sequence) && sequence > 0
    ? { documentId, sequence }
    : null
}

export function createV4ArtifactWorkbench(
  rpc: RpcTransport,
  events: EventTransport,
  http: ArtifactWorkbenchHttpTransport,
): ArtifactWorkbench {
  const listeners = new Set<(change: ArtifactDocumentChange) => void>()
  const seenSequences = new Map<string, number>()
  let wireSubscriptions: TransportSubscription[] = []

  const emit = (payload: unknown): void => {
    const change = documentChange(payload)
    if (!change) return
    if ((seenSequences.get(change.documentId) ?? 0) >= change.sequence) return
    seenSequences.set(change.documentId, change.sequence)
    for (const listener of [...listeners]) listener({ documentId: change.documentId })
  }

  const startLease = (): void => {
    if (wireSubscriptions.length > 0) return
    wireSubscriptions = documentChangeEventContract.wireNames.map(
      event => events.subscribe(event, emit),
    )
  }

  const stopLease = (): void => {
    if (listeners.size > 0) return
    for (const subscription of wireSubscriptions) subscription.close()
    wireSubscriptions = []
    seenSequences.clear()
  }

  const artifactContent = createV4ArtifactContentAccess(http)
  const attachmentContent = createV4AttachmentContentAccess(http)
  return {
    artifacts: createV4ArtifactCatalog(rpc),
    documents: createV4ArtifactDocuments(rpc),
    resources: createV4WorkbenchResources(rpc),
    promptAnnotations: createV4ArtifactPromptAnnotations(rpc),
    content: { ...artifactContent, ...attachmentContent },
    previews: createV4ArtifactPreviews(http),
    ready: () => rpc.ready(),
    subscribeDocumentChanges(listener): ArtifactWorkbenchSubscription {
      listeners.add(listener)
      startLease()
      let closed = false
      return {
        close() {
          if (closed) return
          closed = true
          listeners.delete(listener)
          stopLease()
        },
      }
    },
  }
}
