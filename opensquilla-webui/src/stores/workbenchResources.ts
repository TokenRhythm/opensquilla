import { defineStore } from 'pinia'
import { markRaw, ref, shallowRef, watch } from 'vue'

import type { WorkbenchResourceProvider } from '@/modules/artifactWorkbench'
import type {
  ArtifactMutationOperation,
  ArtifactMutationResolution,
  ArtifactMutationResolutionRequest,
} from '@/types/artifactDocuments'
import type {
  DocumentImportResponse,
  DocumentPublishResponse,
  WorkbenchPreviewResponse,
  WorkbenchResource,
  WorkbenchResourceOpenResponse,
  WorkbenchResourceRef,
  WorkbenchAnnotationRequest,
} from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import {
  artifactMutationOutcomeMayBePending,
  artifactProductClientError,
  classifyArtifactProductError,
} from '@/utils/artifactProductErrors'
import { PendingMutationRequestIds } from '@/utils/mutationRequestIdentity'
import { resolveArtifactMutationBounded } from '@/workbench/artifactMutationRecovery'
import { canonicalWorkbenchResources } from '@/workbench/workbenchResourceItems'
import { MetadataCacheBudget } from '@/workbench/metadataCacheBudget'

export interface WorkbenchResourceSnapshot {
  sessionKey: string
  available: boolean
  loading: boolean
  loaded: boolean
  error: string | null
  resources: WorkbenchResource[]
  totalCount: number
}

function emptySnapshot(sessionKey: string): WorkbenchResourceSnapshot {
  return {
    sessionKey,
    available: false,
    loading: false,
    loaded: false,
    error: null,
    resources: [],
    totalCount: 0,
  }
}

function message(error: unknown): string {
  return classifyArtifactProductError(error).fallbackMessage
}

const PENDING_PUBLISH_STORAGE_PREFIX = 'opensquilla.workbench.pending-publish.'
const pendingPublishKeys = new Map<string, string>()

function identityToken(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (const byte of bytes) {
    first = Math.imul(first ^ byte, 0x01000193) >>> 0
    second = Math.imul(second ^ (byte + 0x9d), 0x85ebca6b) >>> 0
  }
  return `${bytes.length.toString(36)}-${first.toString(16).padStart(8, '0')}${second
    .toString(16)
    .padStart(8, '0')}`
}

function importOperationKey(sessionKey: string, resource: WorkbenchResource): string {
  return JSON.stringify([
    sessionKey,
    resource.resource.type,
    workbenchResourceRefId(resource.resource),
    resource.sha256 || '',
    'copy',
    resource.name,
  ])
}

function publicationOperationKey(
  sessionKey: string,
  documentId: string,
  revisionId: string,
): string {
  return identityToken([sessionKey, documentId, revisionId].join('\u0000'))
}

function browserStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

function pendingPublishIdempotencyKey(operationKey: string): string {
  const memory = pendingPublishKeys.get(operationKey)
  if (memory) return memory
  const storage = browserStorage()
  const storageKey = `${PENDING_PUBLISH_STORAGE_PREFIX}${operationKey}`
  try {
    const persisted = storage?.getItem(storageKey) || ''
    if (/^publish-[A-Za-z0-9._-]{8,200}$/.test(persisted)) {
      pendingPublishKeys.set(operationKey, persisted)
      return persisted
    }
  } catch {
    // Storage is an optional recovery aid. The in-memory key still fences
    // duplicate clicks in this renderer.
  }
  const created = createWorkbenchIdempotencyKey('publish')
  pendingPublishKeys.set(operationKey, created)
  try {
    storage?.setItem(storageKey, created)
  } catch {
    // Keep the in-memory receipt key when persistent storage is unavailable.
  }
  return created
}

function clearPendingPublishIdempotencyKey(operationKey: string) {
  pendingPublishKeys.delete(operationKey)
  try {
    browserStorage()?.removeItem(`${PENDING_PUBLISH_STORAGE_PREFIX}${operationKey}`)
  } catch {
    // The server receipt is authoritative even when browser cleanup fails.
  }
}

export function workbenchResourceKey(resource: WorkbenchResourceRef): string {
  return `${resource.type}:${workbenchResourceRefId(resource)}`
}

export function createWorkbenchIdempotencyKey(prefix: 'import' | 'publish'): string {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${random}`
}

export const useWorkbenchResourcesStore = defineStore('workbenchResources', () => {
  const snapshots = ref<Record<string, WorkbenchResourceSnapshot>>({})
  const provider = shallowRef<WorkbenchResourceProvider | null>(null)
  let providerLifetime = 0
  const requests = new Map<string, AbortController>()
  const imports = new Map<string, {
    provider: WorkbenchResourceProvider
    promise: Promise<DocumentImportResponse>
  }>()
  const opens = new Map<string, {
    provider: WorkbenchResourceProvider
    promise: Promise<WorkbenchResourceOpenResponse | null>
  }>()
  const mutationRequestIds = new PendingMutationRequestIds(64)
  const cacheBudget = new MetadataCacheBudget(12, 2 * 1024 * 1024)
  const pendingMutationOwners = new Map<string, string>()
  function pruneMetadata() {
    cacheBudget.prune(key => requests.has(key) || [...pendingMutationOwners.values()].includes(key), key => {
      const next = { ...snapshots.value }
      delete next[key]
      snapshots.value = next
    })
  }
  function retain(sessionKey: string) {
    const release = cacheBudget.retain(sessionKey)
    return () => { release(); pruneMetadata() }
  }

  // A session may request its inventory before Hello advertises the resource
  // methods. Retry that deferred read when the same provider becomes available;
  // unchanged/empty artifact history must not leave attachments download-only.
  // Cleared sessions are absent here, and load retains the normal request fences.
  watch(() => provider.value?.available() === true, available => {
    if (!available) return
    for (const current of Object.values(snapshots.value)) {
      if (current.loaded && !current.available && !current.loading) {
        void load(current.sessionKey, true).catch(() => undefined)
      }
    }
  })

  async function runRecoverableMutation<T, P extends Readonly<Record<string, unknown>>>(
    options: Parameters<typeof runRecoverableMutationImpl<T, P>>[0],
  ): Promise<T> {
    const release = retain(options.sessionKey)
    try { return await runRecoverableMutationImpl(options) }
    finally { release() }
  }
  async function runRecoverableMutationImpl<
    T,
    P extends Readonly<Record<string, unknown>>,
  >(options: {
    currentProvider: WorkbenchResourceProvider
    logicalKey: string
    requestPrefix: string
    preferredRequestId?: string
    operation: ArtifactMutationOperation
    sessionKey: string
    documentId?: string
    buildPayload: (requestId: string) => P
    execute: (payload: P) => Promise<T>
    onAppliedResolution: (resolution: ArtifactMutationResolution) => Promise<T>
    onRelease?: () => void
  }): Promise<T> {
    const requestId = mutationRequestIds.idFor(
      options.logicalKey,
      options.requestPrefix,
      options.preferredRequestId,
    )
    const wasPending = mutationRequestIds.isPending(options.logicalKey, requestId)
    const payload = mutationRequestIds.pendingPayload<P>(options.logicalKey, requestId)
      || mutationRequestIds.freeze(
        options.logicalKey,
        requestId,
        options.buildPayload(requestId),
      )
    const resolutionRequest: ArtifactMutationResolutionRequest = {
      sessionKey: options.sessionKey,
      operation: options.operation,
      requestId,
      ...(options.documentId ? { documentId: options.documentId } : {}),
    }

    const release = () => {
      mutationRequestIds.release(options.logicalKey, requestId)
      pendingMutationOwners.delete(options.logicalKey)
      options.onRelease?.()
    }
    const pendingError = () => artifactProductClientError('MUTATION_OUTCOME_PENDING')
    const notApplied = (): never => {
      release()
      throw artifactProductClientError('MUTATION_NOT_APPLIED')
    }
    const returnApplied = async (resolution: ArtifactMutationResolution): Promise<T> => {
      // Durable applied is terminal. Never replay a write just to recover its
      // response DTO, and never downgrade that fact when the canonical read
      // model is temporarily unavailable.
      release()
      try {
        return await options.onAppliedResolution(resolution)
      } catch {
        throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
      }
    }
    const resolve = async () => {
      if (!options.currentProvider.resolveMutation) return null
      try {
        return await resolveArtifactMutationBounded(
          request => options.currentProvider.resolveMutation!(request),
          resolutionRequest,
        )
      } catch {
        throw pendingError()
      }
    }

    // A later user retry first asks a current Gateway for the durable outcome.
    // If the Gateway predates resolution, the same frozen request is replayed
    // explicitly; no automatic second write is issued after the first loss.
    if (wasPending && options.currentProvider.resolveMutation) {
      const resolution = await resolve()
      if (resolution?.status === 'not_applied') return notApplied()
      if (resolution?.status === 'pending') throw pendingError()
      if (resolution?.status === 'applied') return returnApplied(resolution)
      // Null is the old-Gateway compatibility signal. Continue to exact replay.
    }

    try {
      const result = await options.execute(payload)
      release()
      return result
    } catch (error) {
      if (!artifactMutationOutcomeMayBePending(error)) {
        release()
        throw error
      }
      mutationRequestIds.markPending(options.logicalKey, requestId)
      pendingMutationOwners.set(options.logicalKey, options.sessionKey)
      if (!wasPending && options.currentProvider.resolveMutation) {
        const resolution = await resolve()
        if (resolution?.status === 'not_applied') return notApplied()
        if (resolution?.status === 'applied') return returnApplied(resolution)
      }
      throw pendingError()
    }
  }

  function setProvider(next: WorkbenchResourceProvider | null) {
    if (provider.value === next) return
    providerLifetime += 1
    abortAll()
    opens.clear()
    provider.value = next ? markRaw(next) : null
  }

  function snapshot(sessionKey: string): WorkbenchResourceSnapshot {
    cacheBudget.touch(sessionKey)
    return snapshots.value[sessionKey] || emptySnapshot(sessionKey)
  }

  function setSnapshot(sessionKey: string, value: WorkbenchResourceSnapshot) {
    snapshots.value = { ...snapshots.value, [sessionKey]: value }
    cacheBudget.set(sessionKey, value)
    pruneMetadata()
  }

  function retire(sessionKey: string) {
    requests.get(sessionKey)?.abort()
    requests.delete(sessionKey)
  }

  async function load(sessionKey: string, force = false): Promise<WorkbenchResourceSnapshot> {
    const current = snapshot(sessionKey)
    if (!sessionKey) return current
    if (!force && current.loaded) return current
    const currentProvider = provider.value
    if (!currentProvider?.available()) {
      const unavailable = { ...current, available: false, loading: false, loaded: true }
      setSnapshot(sessionKey, unavailable)
      return unavailable
    }

    retire(sessionKey)
    const controller = new AbortController()
    requests.set(sessionKey, controller)
    setSnapshot(sessionKey, {
      ...current,
      available: true,
      loading: true,
      error: null,
    })
    try {
      const result = await currentProvider.list(sessionKey, {
        limit: 500,
        signal: controller.signal,
      })
      const loaded: WorkbenchResourceSnapshot = {
        sessionKey,
        available: true,
        loading: false,
        loaded: true,
        error: null,
        resources: result.resources,
        totalCount: result.totalCount,
      }
      if (requests.get(sessionKey) === controller && !controller.signal.aborted) setSnapshot(sessionKey, loaded)
      return loaded
    } catch (error) {
      if (controller.signal.aborted) throw error
      const failed: WorkbenchResourceSnapshot = {
        ...current,
        available: current.available,
        loading: false,
        loaded: current.loaded,
        error: message(error),
      }
      if (requests.get(sessionKey) === controller && !controller.signal.aborted) setSnapshot(sessionKey, failed)
      return failed
    } finally {
      if (requests.get(sessionKey) === controller) requests.delete(sessionKey)
      pruneMetadata()
    }
  }

  function find(sessionKey: string, resource: WorkbenchResourceRef): WorkbenchResource | null {
    const key = workbenchResourceKey(resource)
    return snapshot(sessionKey).resources.find(
      item => workbenchResourceKey(item.resource) === key,
    ) || null
  }

  function navigationResources(sessionKey: string): WorkbenchResource[] {
    return canonicalWorkbenchResources(snapshot(sessionKey).resources)
  }

  function upsertResource(sessionKey: string, resolved: WorkbenchResource) {
    const existing = snapshot(sessionKey)
    const key = workbenchResourceKey(resolved.resource)
    const resources = existing.resources.some(
      item => workbenchResourceKey(item.resource) === key,
    )
      ? existing.resources.map(item => (
          workbenchResourceKey(item.resource) === key ? resolved : item
        ))
      : [...existing.resources, resolved]
    setSnapshot(sessionKey, {
      ...existing,
      available: true,
      resources,
      totalCount: Math.max(existing.totalCount, resources.length),
    })
  }

  async function resolve(
    sessionKey: string,
    resource: WorkbenchResourceRef,
  ): Promise<WorkbenchResource | null> {
    const current = find(sessionKey, resource)
    const currentProvider = provider.value
    if (!currentProvider) return current
    const resolved = await currentProvider.get(sessionKey, resource)
    if (!resolved) return current

    upsertResource(sessionKey, resolved)
    return resolved
  }

  /** Explicit navigation must not treat a stale cached projection as authority. */
  async function resolveFresh(
    sessionKey: string,
    resource: WorkbenchResourceRef,
    isCurrent: () => boolean = () => true,
  ): Promise<WorkbenchResource | null> {
    const currentProvider = provider.value
    const lifetime = providerLifetime
    if (!currentProvider || !isCurrent()) return null
    const resolved = await currentProvider.get(sessionKey, resource)
    if (provider.value !== currentProvider || lifetime !== providerLifetime || !isCurrent()) return null
    if (resolved) upsertResource(sessionKey, resolved)
    return resolved
  }

  async function preview(
    sessionKey: string,
    resource: WorkbenchResourceRef,
  ): Promise<WorkbenchPreviewResponse | null> {
    const currentProvider = provider.value
    if (!currentProvider) return null
    const result = currentProvider.createPreview
      ? await currentProvider.createPreview(sessionKey, resource)
      : await (async () => {
          const resolved = await currentProvider.get(sessionKey, resource)
          if (!resolved?.capabilities.preview) return null
          return {
            resource: resolved,
            preview: {
              protocolVersion: 0,
              mode: 'isolated' as const,
              resource: resolved.resource,
              launchUrl: resolved.downloadUrl,
              sandboxProfile: 'opaque-offline' as const,
              network: false as const,
              adapter: null,
            },
          }
        })()
    if (result) upsertResource(sessionKey, result.resource)
    return result
  }

  async function openCurrent(
    sessionKey: string,
    resource: WorkbenchResource,
  ): Promise<WorkbenchResourceOpenResponse | null> {
    const currentProvider = provider.value
    if (!currentProvider?.open) return null
    const operationKey = JSON.stringify([
      sessionKey,
      resource.resource.type,
      workbenchResourceRefId(resource.resource),
      'open-current',
    ])
    const pending = opens.get(operationKey)
    if (pending?.provider === currentProvider) return pending.promise

    const promise = (async () => {
      const result = await runRecoverableMutation({
        currentProvider,
        logicalKey: operationKey,
        requestPrefix: 'workbench-open',
        operation: 'workbench.resources.open',
        sessionKey,
        buildPayload: idempotencyKey => ({
          sessionKey,
          resource: resource.resource,
          request: {
            intent: 'edit-current' as const,
            ...(resource.sha256 ? { expectedSha256: resource.sha256 } : {}),
            idempotencyKey,
          },
        }),
        execute: payload => currentProvider.open!(
          payload.sessionKey as string,
          payload.resource as WorkbenchResourceRef,
          payload.request as {
            intent: 'edit-current'
            expectedSha256?: string
            idempotencyKey: string
          },
        ),
        onAppliedResolution: async resolution => {
          const document = resolution.document
          const revision = resolution.revision
          if (
            !document
            || !revision
            || revision.documentId !== document.documentId
            || revision.revisionId !== document.headRevisionId
          ) throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
          const documentRef: WorkbenchResourceRef = {
            type: 'document',
            documentId: document.documentId,
            id: document.documentId,
          }
          const resolvedResource = await currentProvider.get(sessionKey, documentRef)
          if (!resolvedResource) throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
          upsertResource(sessionKey, resolvedResource)
          const materialized = resource.resource.type !== 'document'
          return {
            disposition: 'document' as const,
            resolution: { status: materialized ? 'materialized' as const : 'current' as const },
            resource: resolvedResource,
            document,
            revision,
            materialized,
          }
        },
      })
      if (!result) return null
      upsertResource(sessionKey, result.resource)
      if (result.disposition === 'document' && result.materialized) {
        await load(sessionKey, true)
      }
      return result
    })()
    opens.set(operationKey, { provider: currentProvider, promise })
    try {
      return await promise
    } finally {
      if (opens.get(operationKey)?.promise === promise) opens.delete(operationKey)
    }
  }

  async function prepareAnnotation(
    sessionKey: string,
    request: WorkbenchAnnotationRequest,
  ): Promise<WorkbenchResourceOpenResponse | null> {
    const currentProvider = provider.value
    if (!currentProvider?.prepareAnnotation) return null
    const operationKey = JSON.stringify([sessionKey, 'annotate',
      request.resourceRef ? workbenchResourceKey(request.resourceRef) : request.workspaceFile,
      request.expectedSha256 || ''])
    const pending = opens.get(operationKey)
    if (pending?.provider === currentProvider) return pending.promise
    const idempotencyKey = mutationRequestIds.idFor(operationKey, 'workbench-annotate', request.idempotencyKey)
    const payload: WorkbenchAnnotationRequest = request.resourceRef
      ? { resourceRef: { ...request.resourceRef }, expectedSha256: request.expectedSha256, idempotencyKey }
      : { workspaceFile: { ...request.workspaceFile }, idempotencyKey }
    const promise = (async () => {
      const result = await currentProvider.prepareAnnotation!(sessionKey, payload)
      mutationRequestIds.release(operationKey, idempotencyKey)
      if (result && provider.value === currentProvider) upsertResource(sessionKey, result.resource)
      return result
    })()
    opens.set(operationKey, { provider: currentProvider, promise })
    try { return await promise }
    finally { if (opens.get(operationKey)?.promise === promise) opens.delete(operationKey) }
  }

  async function importDocument(
    sessionKey: string,
    resource: WorkbenchResource,
    idempotencyKey?: string,
  ): Promise<DocumentImportResponse> {
    const currentProvider = provider.value
    if (!currentProvider || !resource.capabilities.edit) {
      throw new Error('This resource cannot be imported as an editable document.')
    }
    if (!resource.sha256) {
      throw new Error('This resource does not have a verified source digest.')
    }
    const expectedSha256 = resource.sha256
    const operationKey = importOperationKey(sessionKey, resource)
    const pending = imports.get(operationKey)
    if (pending?.provider === currentProvider) return pending.promise

    const promise = (async () => {
      const result = await runRecoverableMutation({
        currentProvider,
        logicalKey: operationKey,
        requestPrefix: 'document-import',
        ...(idempotencyKey ? { preferredRequestId: idempotencyKey } : {}),
        operation: 'document.import',
        sessionKey,
        buildPayload: requestId => ({
          sessionKey,
          source: resource.resource,
          expectedSha256,
          idempotencyKey: requestId,
          name: resource.name,
        }),
        execute: payload => currentProvider.importDocument({
          sessionKey: payload.sessionKey as string,
          source: payload.source as WorkbenchResourceRef,
          expectedSha256: payload.expectedSha256 as string,
          idempotencyKey: payload.idempotencyKey as string,
          name: payload.name as string,
        }),
        onAppliedResolution: async resolution => {
          if (
            !resolution.document
            || !resolution.revision
            || resolution.revision.documentId !== resolution.document.documentId
          ) throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
          return {
            document: resolution.document,
            revision: resolution.revision,
          }
        },
      })
      await load(sessionKey, true)
      return result
    })()
    imports.set(operationKey, { provider: currentProvider, promise })
    try {
      return await promise
    } finally {
      if (imports.get(operationKey)?.promise === promise) imports.delete(operationKey)
    }
  }

  async function publishDocument(
    sessionKey: string,
    documentId: string,
    revisionId: string,
    name?: string,
    idempotencyKey?: string,
  ): Promise<DocumentPublishResponse | null> {
    const currentProvider = provider.value
    if (!currentProvider) throw new Error('Document publication is unavailable.')
    const operationKey = publicationOperationKey(sessionKey, documentId, revisionId)
    const persistedRequestId = idempotencyKey || pendingPublishIdempotencyKey(operationKey)
    const result = await runRecoverableMutation({
      currentProvider,
      logicalKey: `publish:${operationKey}`,
      requestPrefix: 'document-publish',
      preferredRequestId: persistedRequestId,
      operation: 'document.publish',
      sessionKey,
      documentId,
      buildPayload: requestId => ({
        sessionKey,
        documentId,
        revisionId,
        idempotencyKey: requestId,
        ...(name ? { name } : {}),
      }),
      execute: payload => currentProvider.publishDocument({
        sessionKey: payload.sessionKey as string,
        documentId: payload.documentId as string,
        revisionId: payload.revisionId as string,
        idempotencyKey: payload.idempotencyKey as string,
        ...(typeof payload.name === 'string' ? { name: payload.name } : {}),
      }),
      onAppliedResolution: async () => null,
      ...(!idempotencyKey
        ? { onRelease: () => clearPendingPublishIdempotencyKey(operationKey) }
        : {}),
    })
    await load(sessionKey, true)
    return result
  }

  function clearSession(sessionKey: string) {
    retire(sessionKey)
    cacheBudget.remove(sessionKey)
    const next = { ...snapshots.value }
    delete next[sessionKey]
    snapshots.value = next
  }

  function abortAll() {
    for (const sessionKey of requests.keys()) retire(sessionKey)
  }

  function reset() {
    providerLifetime += 1
    abortAll()
    opens.clear()
    mutationRequestIds.clear()
    pendingMutationOwners.clear()
    cacheBudget.clear()
    snapshots.value = {}
  }

  return {
    snapshots,
    retain,
    setProvider,
    snapshot,
    load,
    find,
    navigationResources,
    resolve,
    resolveFresh,
    preview,
    openCurrent,
    prepareAnnotation,
    importDocument,
    publishDocument,
    clearSession,
    reset,
  }
})
