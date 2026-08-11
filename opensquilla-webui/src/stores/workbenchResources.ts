import { defineStore } from 'pinia'
import { markRaw, ref, shallowRef } from 'vue'

import type {
  DocumentImportResponse,
  DocumentPublishResponse,
  WorkbenchPreviewResponse,
  WorkbenchResource,
  WorkbenchResourceRef,
} from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import type { WorkbenchResourceProvider } from '@/workbench/workbenchResourceProvider'

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
  return error instanceof Error && error.message
    ? error.message
    : 'Workbench resources are unavailable.'
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

function stableImportIdempotencyKey(
  sessionKey: string,
  resource: WorkbenchResource,
): string {
  return `import-${identityToken([
    sessionKey,
    resource.resource.type,
    workbenchResourceRefId(resource.resource),
    resource.sha256 || '',
    resource.name,
    'copy',
  ].join('\u0000'))}`
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
  const requests = new Map<string, AbortController>()
  const generations = new Map<string, number>()
  const imports = new Map<string, {
    provider: WorkbenchResourceProvider
    promise: Promise<DocumentImportResponse>
  }>()

  function setProvider(next: WorkbenchResourceProvider | null) {
    if (provider.value === next) return
    abortAll()
    provider.value = next ? markRaw(next) : null
  }

  function snapshot(sessionKey: string): WorkbenchResourceSnapshot {
    return snapshots.value[sessionKey] || emptySnapshot(sessionKey)
  }

  function setSnapshot(sessionKey: string, value: WorkbenchResourceSnapshot) {
    snapshots.value = { ...snapshots.value, [sessionKey]: value }
  }

  function retire(sessionKey: string): number {
    requests.get(sessionKey)?.abort()
    requests.delete(sessionKey)
    const generation = (generations.get(sessionKey) || 0) + 1
    generations.set(sessionKey, generation)
    return generation
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

    const generation = retire(sessionKey)
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
      if (generations.get(sessionKey) === generation) setSnapshot(sessionKey, loaded)
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
      if (generations.get(sessionKey) === generation) setSnapshot(sessionKey, failed)
      return failed
    } finally {
      if (requests.get(sessionKey) === controller) requests.delete(sessionKey)
    }
  }

  function find(sessionKey: string, resource: WorkbenchResourceRef): WorkbenchResource | null {
    const key = workbenchResourceKey(resource)
    return snapshot(sessionKey).resources.find(
      item => workbenchResourceKey(item.resource) === key,
    ) || null
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
      const result = await currentProvider.importDocument({
        sessionKey,
        source: resource.resource,
        expectedSha256,
        idempotencyKey: idempotencyKey || stableImportIdempotencyKey(sessionKey, resource),
        name: resource.name,
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
  ): Promise<DocumentPublishResponse> {
    const currentProvider = provider.value
    if (!currentProvider) throw new Error('Document publication is unavailable.')
    const operationKey = publicationOperationKey(sessionKey, documentId, revisionId)
    const receiptKey = idempotencyKey || pendingPublishIdempotencyKey(operationKey)
    const result = await currentProvider.publishDocument({
      sessionKey,
      documentId,
      revisionId,
      idempotencyKey: receiptKey,
      name,
    })
    if (!idempotencyKey) clearPendingPublishIdempotencyKey(operationKey)
    await load(sessionKey, true)
    return result
  }

  function clearSession(sessionKey: string) {
    retire(sessionKey)
    const next = { ...snapshots.value }
    delete next[sessionKey]
    snapshots.value = next
  }

  function abortAll() {
    for (const sessionKey of requests.keys()) retire(sessionKey)
  }

  function reset() {
    abortAll()
    snapshots.value = {}
  }

  return {
    snapshots,
    setProvider,
    snapshot,
    load,
    find,
    resolve,
    preview,
    importDocument,
    publishDocument,
    clearSession,
    reset,
  }
})
