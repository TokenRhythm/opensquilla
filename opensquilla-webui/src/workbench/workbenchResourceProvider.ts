import type { RpcCallOptions } from '@/lib/rpc'
import type { ArtifactPayload } from '@/types/rpc'
import type {
  ArtifactMutationResolution,
  ArtifactMutationResolutionRequest,
} from '@/types/artifactDocuments'
import type {
  DocumentImportResponse,
  DocumentOperationReceipt,
  DocumentPublication,
  DocumentPublishResponse,
  DocumentSourceBinding,
  WorkbenchResource,
  WorkbenchResourceCapabilities,
  WorkbenchResourceRef,
  WorkbenchResourceRelations,
  WorkbenchResourcesListResponse,
  WorkbenchResourceType,
  WorkbenchPreviewResponse,
  WorkbenchResourceOpenResponse,
} from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import {
  normalizeArtifactDocument,
  normalizeArtifactRevision,
} from './artifactDocumentProvider'

export const WORKBENCH_RESOURCE_RPC_METHODS = {
  list: 'workbench.resources.list',
  get: 'workbench.resources.get',
  open: 'workbench.resources.open',
  createPreview: 'workbench.previews.create',
  importDocument: 'documents.import',
  publishDocument: 'documents.publish',
  mutationResolve: 'artifacts.mutations.resolve',
} as const

type WorkbenchResourceRpc = {
  supportsMethod?: (method: string) => boolean
  markMethodUnavailable?: (method: string) => void
  call: (
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ) => Promise<unknown>
}

export interface WorkbenchResourceProvider {
  available(): boolean
  list(
    sessionKey: string,
    options?: { types?: WorkbenchResourceType[]; limit?: number; signal?: AbortSignal },
  ): Promise<WorkbenchResourcesListResponse>
  get(
    sessionKey: string,
    resource: WorkbenchResourceRef,
    signal?: AbortSignal,
  ): Promise<WorkbenchResource | null>
  open?(
    sessionKey: string,
    resource: WorkbenchResourceRef,
    request: {
      intent: 'edit-current'
      expectedSha256?: string
      idempotencyKey: string
    },
    signal?: AbortSignal,
  ): Promise<WorkbenchResourceOpenResponse | null>
  createPreview?(
    sessionKey: string,
    resource: WorkbenchResourceRef,
    signal?: AbortSignal,
  ): Promise<WorkbenchPreviewResponse | null>
  importDocument(request: {
    sessionKey: string
    source: WorkbenchResourceRef
    expectedSha256: string
    idempotencyKey: string
    name?: string
  }, signal?: AbortSignal): Promise<DocumentImportResponse>
  publishDocument(request: {
    sessionKey: string
    documentId: string
    revisionId: string
    idempotencyKey: string
    name?: string
  }, signal?: AbortSignal): Promise<DocumentPublishResponse>
  /** Null means the connected Gateway predates mutation outcome resolution. */
  resolveMutation?(
    request: ArtifactMutationResolutionRequest,
    signal?: AbortSignal,
  ): Promise<ArtifactMutationResolution | null>
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function valueAt(raw: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    if (raw[key] !== undefined) return raw[key]
  }
  return undefined
}

function stringAt(raw: Record<string, unknown>, ...keys: string[]): string {
  const value = valueAt(raw, ...keys)
  return typeof value === 'string' ? value.trim() : ''
}

function boolAt(raw: Record<string, unknown>, ...keys: string[]): boolean {
  return valueAt(raw, ...keys) === true
}

function numberAt(raw: Record<string, unknown>, ...keys: string[]): number | undefined {
  const value = valueAt(raw, ...keys)
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function normalizeRef(value: unknown): WorkbenchResourceRef | null {
  const raw = record(value)
  if (!raw) return null
  const type = stringAt(raw, 'type', 'kind') as WorkbenchResourceType
  const legacyId = stringAt(raw, 'id', 'resourceId', 'resource_id')
  let canonicalId = ''
  switch (type) {
    case 'attachment':
      canonicalId = stringAt(raw, 'attachmentId', 'attachment_id')
      break
    case 'document':
      canonicalId = stringAt(raw, 'documentId', 'document_id')
      break
    case 'deliverable':
      canonicalId = stringAt(raw, 'artifactId', 'artifact_id')
      break
    case 'url':
      canonicalId = stringAt(raw, 'urlId', 'url_id')
      break
    default:
      return null
  }
  if (canonicalId && legacyId && canonicalId !== legacyId) return null
  const id = canonicalId || legacyId
  if (!id) return null
  switch (type) {
    case 'attachment':
      return { type, attachmentId: id, id }
    case 'document':
      return { type, documentId: id, id }
    case 'deliverable':
      return { type, artifactId: id, id }
    case 'url':
      return { type, urlId: id, id }
  }
}

function serializeRef(value: WorkbenchResourceRef): Record<string, string> {
  const normalized = normalizeRef(value)
  if (!normalized) throw new Error('The workbench resource identity is invalid.')
  return normalized as Record<string, string>
}

function normalizeCapabilities(value: unknown): WorkbenchResourceCapabilities {
  const raw = record(value) || {}
  const preview = boolAt(raw, 'preview')
  const legacyEdit = boolAt(raw, 'edit')
  const hasManualEdit = valueAt(raw, 'manualEdit', 'manual_edit') !== undefined
  const manualEdit = boolAt(raw, 'manualEdit', 'manual_edit')
    || (!hasManualEdit && legacyEdit)
  const agentEdit = boolAt(raw, 'agentEdit', 'agent_edit')
  const selectionContext = boolAt(
    raw,
    'selectionContext',
    'selection_context',
  )
  const edit = legacyEdit || manualEdit || agentEdit
  const legacyReason = stringAt(
    raw,
    'reasonCode',
    'reason_code',
    'unavailableReason',
    'unavailable_reason',
  ) || null
  const editReasonCode = stringAt(
    raw,
    'editReasonCode',
    'edit_reason_code',
  ) || (!edit ? legacyReason : null)
  const previewReasonCode = stringAt(
    raw,
    'previewReasonCode',
    'preview_reason_code',
  ) || (!preview ? legacyReason || editReasonCode : null)
  return {
    preview,
    download: boolAt(raw, 'download'),
    selectionContext,
    manualEdit,
    agentEdit,
    edit,
    publish: boolAt(raw, 'publish'),
    previewReasonCode,
    editReasonCode,
    reasonCode: legacyReason || editReasonCode || previewReasonCode,
  }
}

function normalizeRelations(value: unknown): WorkbenchResourceRelations {
  const raw = record(value) || {}
  const source = normalizeRef(valueAt(raw, 'source'))
  return {
    documentId: stringAt(raw, 'documentId', 'document_id') || undefined,
    headRevisionId: stringAt(raw, 'headRevisionId', 'head_revision_id') || undefined,
    headArtifactId: stringAt(raw, 'headArtifactId', 'head_artifact_id') || undefined,
    source: source || undefined,
    deliverableId: stringAt(raw, 'deliverableId', 'deliverable_id') || undefined,
    publishedRevisionId: stringAt(
      raw,
      'publishedRevisionId',
      'published_revision_id',
    ) || undefined,
  }
}

export function normalizeWorkbenchResource(value: unknown): WorkbenchResource | null {
  const raw = record(value)
  if (!raw) return null
  const resource = normalizeRef(valueAt(raw, 'resource') || raw)
  const name = stringAt(raw, 'name')
  const mime = stringAt(raw, 'mime', 'mediaType', 'media_type')
  if (!resource || !name || !mime) return null
  return {
    resource,
    name,
    mime,
    size: numberAt(raw, 'size', 'byteSize', 'byte_size'),
    sha256: stringAt(raw, 'sha256') || undefined,
    createdAt: valueAt(raw, 'createdAt', 'created_at') as number | string | null | undefined,
    updatedAt: valueAt(raw, 'updatedAt', 'updated_at') as number | string | null | undefined,
    downloadUrl: stringAt(raw, 'downloadUrl', 'download_url') || undefined,
    capabilities: normalizeCapabilities(valueAt(raw, 'capabilities')),
    relations: normalizeRelations(valueAt(raw, 'relations')),
  }
}

function normalizeReceipt(value: unknown): DocumentOperationReceipt | null {
  const raw = record(value)
  if (!raw) return null
  const attemptId = stringAt(raw, 'attemptId', 'attempt_id')
  const idempotencyKey = stringAt(raw, 'idempotencyKey', 'idempotency_key')
  const requestId = stringAt(raw, 'requestId', 'request_id') || idempotencyKey
  const status = stringAt(raw, 'status') as DocumentOperationReceipt['status']
  if (
    !attemptId
    || !requestId
    || !idempotencyKey
    || !['applied', 'failed', 'ambiguous'].includes(status)
  ) {
    return null
  }
  return {
    attemptId,
    requestId,
    idempotencyKey,
    status,
    replayed: boolAt(raw, 'replayed'),
    failureCode: stringAt(raw, 'failureCode', 'failure_code') || null,
  }
}

function normalizeBinding(value: unknown): DocumentSourceBinding | null {
  const raw = record(value)
  if (!raw) return null
  const source = normalizeRef(valueAt(raw, 'source'))
  // V036 initially used the repository-native `id` field on the wire. Keep
  // accepting it while preferring the explicit public DTO spelling.
  const bindingId = stringAt(raw, 'bindingId', 'binding_id', 'id')
  const documentId = stringAt(raw, 'documentId', 'document_id')
  const sourceSha256 = stringAt(raw, 'sourceSha256', 'source_sha256')
  if (!source || !bindingId || !documentId || !sourceSha256) return null
  return { bindingId, documentId, source, sourceSha256, mode: 'copy' }
}

function isMethodNotFound(error: unknown): boolean {
  const code = (error as { code?: unknown } | null)?.code
  return code === 'METHOD_NOT_FOUND'
    || /method not found/i.test(error instanceof Error ? error.message : String(error || ''))
}

export function createRpcWorkbenchResourceProvider(
  rpc: WorkbenchResourceRpc,
): WorkbenchResourceProvider {
  const supports = (method: string) => rpc.supportsMethod?.(method) !== false

  async function call<T>(
    method: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<T> {
    if (!supports(method)) throw new Error('Workbench resource API is unavailable.')
    try {
      return await rpc.call(method, params, { signal, timeoutMs: 15_000 }) as T
    } catch (error) {
      if (isMethodNotFound(error)) rpc.markMethodUnavailable?.(method)
      throw error
    }
  }

  return {
    available: () => supports(WORKBENCH_RESOURCE_RPC_METHODS.list),
    async list(sessionKey, options = {}) {
      if (!supports(WORKBENCH_RESOURCE_RPC_METHODS.list)) {
        return { resources: [], totalCount: 0 }
      }
      const resources = new Map<string, WorkbenchResource>()
      const visitedCursors = new Set<string>()
      let cursor = ''
      let totalCount = 0
      while (true) {
        const response = await call<Record<string, unknown>>(
          WORKBENCH_RESOURCE_RPC_METHODS.list,
          {
            sessionKey,
            ...(options.types?.length ? { types: options.types } : {}),
            ...(options.limit ? { limit: options.limit } : {}),
            ...(cursor ? { cursor } : {}),
          },
          options.signal,
        )
        const page = Array.isArray(response.resources)
          ? response.resources
              .map(normalizeWorkbenchResource)
              .filter((item): item is WorkbenchResource => item !== null)
          : []
        for (const item of page) {
          resources.set(
            `${item.resource.type}:${workbenchResourceRefId(item.resource)}`,
            item,
          )
        }
        totalCount = Math.max(
          totalCount,
          numberAt(response, 'totalCount', 'total_count') ?? resources.size,
        )
        const hasMore = boolAt(response, 'hasMore', 'has_more')
        const nextCursor = stringAt(response, 'nextCursor', 'next_cursor')
        if (!hasMore) break
        if (!nextCursor || visitedCursors.has(nextCursor)) {
          throw new Error('Workbench resource pagination did not advance.')
        }
        visitedCursors.add(nextCursor)
        cursor = nextCursor
      }
      return { resources: [...resources.values()], totalCount }
    },
    async get(sessionKey, resource, signal) {
      if (!supports(WORKBENCH_RESOURCE_RPC_METHODS.get)) return null
      const response = await call<Record<string, unknown>>(
        WORKBENCH_RESOURCE_RPC_METHODS.get,
        { sessionKey, resourceRef: serializeRef(resource) },
        signal,
      )
      return normalizeWorkbenchResource(response.resource)
    },
    async open(sessionKey, resource, request, signal) {
      if (!supports(WORKBENCH_RESOURCE_RPC_METHODS.open)) return null
      let response: Record<string, unknown>
      try {
        response = await call<Record<string, unknown>>(
          WORKBENCH_RESOURCE_RPC_METHODS.open,
          {
            sessionKey,
            resourceRef: serializeRef(resource),
            intent: request.intent,
            ...(request.expectedSha256
              ? { expectedSha256: request.expectedSha256 }
              : {}),
            idempotencyKey: request.idempotencyKey,
          },
          signal,
        )
      } catch (error) {
        // Mixed-version Gateways keep the existing preview/import fallback.
        if (isMethodNotFound(error)) return null
        throw error
      }
      const resolved = normalizeWorkbenchResource(response.resource)
      const resolution = record(response.resolution)
      const resolutionStatus = resolution ? stringAt(resolution, 'status') : ''
      const disposition = stringAt(response, 'disposition')
        || (resolutionStatus === 'readonly' ? 'readonly' : 'document')
      if (!resolved) throw new Error('The workbench open response is invalid.')
      if (disposition === 'readonly') {
        const reasonCode = stringAt(response, 'reasonCode', 'reason_code')
          || resolved.capabilities.editReasonCode
          || resolved.capabilities.reasonCode
          || 'format_edit_not_supported'
        return {
          disposition,
          resolution: { status: 'readonly' },
          resource: {
            ...resolved,
            capabilities: {
              ...resolved.capabilities,
              manualEdit: false,
              edit: resolved.capabilities.agentEdit,
              editReasonCode: reasonCode,
              reasonCode,
            },
          },
          reasonCode,
          materialized: false,
        }
      }
      if (disposition !== 'document') {
        throw new Error('The workbench open disposition is invalid.')
      }
      const document = normalizeArtifactDocument(response.document, undefined, sessionKey)
      const revision = normalizeArtifactRevision(response.revision)
      const binding = response.binding === undefined
        ? null
        : normalizeBinding(response.binding)
      if (
        !document
        || !revision
        || revision.documentId !== document.documentId
        || revision.revisionId !== document.headRevisionId
        || resolved.resource.type !== 'document'
        || workbenchResourceRefId(resolved.resource) !== document.documentId
        || (response.binding !== undefined && !binding)
      ) {
        throw new Error('The current workbench document response is invalid.')
      }
      return {
        disposition,
        resolution: {
          status: resolutionStatus === 'materialized' ? 'materialized' : 'current',
        },
        resource: resolved,
        document,
        revision,
        ...(binding ? { binding } : {}),
        materialized: boolAt(response, 'materialized'),
      }
    },
    async createPreview(sessionKey, resource, signal) {
      if (!supports(WORKBENCH_RESOURCE_RPC_METHODS.createPreview)) {
        const fallback = await this.get(sessionKey, resource, signal)
        if (!fallback?.capabilities.preview) return null
        return {
          resource: fallback,
          preview: {
            protocolVersion: 0,
            mode: 'isolated',
            resource: fallback.resource,
            launchUrl: fallback.downloadUrl,
            sandboxProfile: 'opaque-offline',
            network: false,
            adapter: null,
          },
        }
      }
      let response: Record<string, unknown>
      try {
        response = await call<Record<string, unknown>>(
          WORKBENCH_RESOURCE_RPC_METHODS.createPreview,
          { sessionKey, resourceRef: serializeRef(resource), mode: 'isolated' },
          signal,
        )
      } catch (error) {
        if (!isMethodNotFound(error)) throw error
        const fallback = await this.get(sessionKey, resource, signal)
        if (!fallback?.capabilities.preview) return null
        return {
          resource: fallback,
          preview: {
            protocolVersion: 0,
            mode: 'isolated',
            resource: fallback.resource,
            launchUrl: fallback.downloadUrl,
            sandboxProfile: 'opaque-offline',
            network: false,
            adapter: null,
          },
        }
      }
      const resolved = normalizeWorkbenchResource(response.resource)
      const preview = record(response.preview)
      const previewResource = normalizeRef(preview?.resource)
      if (
        !resolved
        || !resolved.capabilities.preview
        || !preview
        || !previewResource
        || previewResource.type !== resolved.resource.type
        || workbenchResourceRefId(previewResource)
          !== workbenchResourceRefId(resolved.resource)
        || stringAt(preview, 'mode') !== 'isolated'
        || stringAt(preview, 'sandboxProfile', 'sandbox_profile') !== 'opaque-offline'
        || valueAt(preview, 'network') !== false
      ) {
        throw new Error('The workbench preview descriptor is invalid.')
      }
      return {
        resource: resolved,
        preview: {
          protocolVersion: numberAt(preview, 'protocolVersion', 'protocol_version') ?? 1,
          mode: 'isolated',
          resource: previewResource,
          launchUrl: stringAt(preview, 'launchUrl', 'launch_url') || undefined,
          sandboxProfile: 'opaque-offline',
          network: false,
          adapter: record(preview.adapter),
        },
      }
    },
    async importDocument(request, signal) {
      const response = await call<Record<string, unknown>>(
        WORKBENCH_RESOURCE_RPC_METHODS.importDocument,
        {
          sessionKey: request.sessionKey,
          source: serializeRef(request.source),
          mode: 'copy',
          expectedSha256: request.expectedSha256,
          clientRequestId: request.idempotencyKey,
          idempotencyKey: request.idempotencyKey,
          ...(request.name ? { name: request.name } : {}),
        },
        signal,
      )
      const document = normalizeArtifactDocument(response.document, undefined, request.sessionKey)
      const revision = normalizeArtifactRevision(response.revision)
      const binding = normalizeBinding(response.binding)
      const receipt = normalizeReceipt(response.receipt)
      if (!document || !revision || !binding || !receipt || receipt.status !== 'applied') {
        throw new Error('The document import receipt is invalid.')
      }
      return { document, revision, binding, receipt }
    },
    async publishDocument(request, signal) {
      const response = await call<Record<string, unknown>>(
        WORKBENCH_RESOURCE_RPC_METHODS.publishDocument,
        {
          sessionKey: request.sessionKey,
          documentId: request.documentId,
          revisionId: request.revisionId,
          clientRequestId: request.idempotencyKey,
          idempotencyKey: request.idempotencyKey,
          ...(request.name ? { name: request.name } : {}),
        },
        signal,
      )
      const deliverable = record(response.deliverable) as ArtifactPayload | null
      const publicationRaw = record(response.publication)
      const receipt = normalizeReceipt(response.receipt)
      if (!deliverable || !publicationRaw || !receipt || receipt.status !== 'applied') {
        throw new Error('The document publication receipt is invalid.')
      }
      const publication: DocumentPublication = {
        // Accept the initial V036 repository-native aliases as well as the
        // explicit public DTO fields so mixed desktop/gateway versions remain
        // interoperable.
        publicationId: stringAt(publicationRaw, 'publicationId', 'publication_id', 'id'),
        documentId: stringAt(publicationRaw, 'documentId', 'document_id'),
        revisionId: stringAt(publicationRaw, 'revisionId', 'revision_id'),
        artifactId: stringAt(
          publicationRaw,
          'artifactId',
          'artifact_id',
          'deliverableId',
          'deliverable_id',
        ),
        createdAt: valueAt(publicationRaw, 'createdAt', 'created_at') as (
          number | string | null | undefined
        ),
      }
      if (
        !publication.publicationId
        || !publication.documentId
        || !publication.revisionId
        || !publication.artifactId
      ) throw new Error('The document publication is invalid.')
      return { deliverable, publication, receipt }
    },
    async resolveMutation(request, signal) {
      if (!supports(WORKBENCH_RESOURCE_RPC_METHODS.mutationResolve)) return null
      let response: Record<string, unknown>
      try {
        response = await call<Record<string, unknown>>(
          WORKBENCH_RESOURCE_RPC_METHODS.mutationResolve,
          { ...request },
          signal,
        )
      } catch (error) {
        if (isMethodNotFound(error)) return null
        throw error
      }
      const status = stringAt(response, 'status')
      if (status !== 'applied' && status !== 'not_applied' && status !== 'pending') {
        throw new Error('Invalid page update resolution response')
      }
      const rawResult = record(response.result)
      const document = normalizeArtifactDocument(response.document, undefined, request.sessionKey)
      const rawDocument = record(response.document)
      const revision = normalizeArtifactRevision(rawDocument?.head)
      const documentId = rawResult ? stringAt(rawResult, 'documentId') : ''
      const revisionId = rawResult ? stringAt(rawResult, 'revisionId') : ''
      const sha256 = rawResult ? stringAt(rawResult, 'sha256') : ''
      const rawStateRevision = rawResult ? Number(rawResult.stateRevision) : NaN
      const rawRetryAfterMs = valueAt(response, 'retryAfterMs')
      const retryAfterMs = Number(rawRetryAfterMs)
      return {
        status,
        retryAfterMs: rawRetryAfterMs !== null
          && rawRetryAfterMs !== undefined
          && Number.isFinite(retryAfterMs)
          ? Math.max(0, retryAfterMs)
          : null,
        result: documentId
          && revisionId
          && /^[0-9a-f]{64}$/.test(sha256)
          && Number.isFinite(rawStateRevision)
          ? {
              documentId,
              revisionId,
              sha256,
              stateRevision: Math.max(1, rawStateRevision),
            }
          : null,
        ...(document ? { document } : {}),
        ...(revision ? { revision } : {}),
      }
    },
  }
}
