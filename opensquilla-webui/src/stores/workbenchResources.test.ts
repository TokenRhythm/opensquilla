import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { WorkbenchResource } from '@/types/workbenchResources'
import type { WorkbenchResourceProvider } from '@/workbench/workbenchResourceProvider'
import { useWorkbenchResourcesStore } from './workbenchResources'

const attachment: WorkbenchResource = {
  resource: { type: 'attachment', id: 'att-a' },
  name: 'page.html',
  mime: 'text/html',
  sha256: 'a'.repeat(64),
  capabilities: { preview: true, download: true, edit: true, publish: false },
  relations: {},
}

describe('workbench resources store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('loads read-only projections without manufacturing fallback resources', async () => {
    const provider: WorkbenchResourceProvider = {
      available: () => true,
      list: vi.fn(async () => ({ resources: [attachment], totalCount: 1 })),
      get: vi.fn(),
      importDocument: vi.fn(),
      publishDocument: vi.fn(),
    }
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)

    const result = await store.load('session-a')

    expect(result.resources).toEqual([attachment])
    expect(store.find('session-a', attachment.resource)).toEqual(attachment)
    expect(provider.list).toHaveBeenCalledTimes(1)
  })

  it('resolves an inline attachment URL only when the user opens it', async () => {
    const resolved = {
      ...attachment,
      downloadUrl: 'data:text/html;base64,PGgxPkhlbGxvPC9oMT4=',
    }
    const provider: WorkbenchResourceProvider = {
      available: () => true,
      list: vi.fn(async () => ({ resources: [attachment], totalCount: 1 })),
      get: vi.fn(async () => resolved),
      importDocument: vi.fn(),
      publishDocument: vi.fn(),
    }
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)
    await store.load('session-a')

    expect(store.find('session-a', attachment.resource)?.downloadUrl).toBeUndefined()
    await expect(store.resolve('session-a', attachment.resource)).resolves.toEqual(resolved)
    expect(provider.get).toHaveBeenCalledWith('session-a', attachment.resource)
    expect(store.find('session-a', attachment.resource)?.downloadUrl).toBe(resolved.downloadUrl)
  })

  it('imports only after an explicit edit action and refreshes the projection', async () => {
    const list = vi.fn()
      .mockResolvedValueOnce({ resources: [attachment], totalCount: 1 })
      .mockResolvedValueOnce({
        resources: [{
          ...attachment,
          resource: { type: 'document', id: 'doc-a' },
          relations: { documentId: 'doc-a', headRevisionId: 'rev-a' },
        }],
        totalCount: 1,
      })
    const importDocument = vi.fn(async (
      _request: Parameters<WorkbenchResourceProvider['importDocument']>[0],
    ) => ({
      document: { documentId: 'doc-a' },
      revision: { revisionId: 'rev-a' },
      binding: { bindingId: 'bind-a' },
      receipt: { status: 'applied' },
    }))
    const provider = {
      available: () => true,
      list,
      get: vi.fn(),
      importDocument,
      publishDocument: vi.fn(),
    } as unknown as WorkbenchResourceProvider
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)
    await store.load('session-a')

    await store.importDocument('session-a', attachment, 'request-a')

    expect(importDocument).toHaveBeenCalledWith(expect.objectContaining({
      sessionKey: 'session-a',
      source: attachment.resource,
      expectedSha256: attachment.sha256,
      idempotencyKey: 'request-a',
    }))
    expect(list).toHaveBeenCalledTimes(2)
  })

  it('derives one stable import receipt key for the same immutable source', async () => {
    const importDocument = vi.fn(async (
      _request: Parameters<WorkbenchResourceProvider['importDocument']>[0],
    ) => ({
      document: { documentId: 'doc-stable' },
      revision: { revisionId: 'rev-stable' },
      binding: { bindingId: 'bind-stable' },
      receipt: { status: 'applied' },
    }))
    const provider = {
      available: () => true,
      list: vi.fn(async () => ({ resources: [attachment], totalCount: 1 })),
      get: vi.fn(),
      importDocument,
      publishDocument: vi.fn(),
    } as unknown as WorkbenchResourceProvider
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)

    await store.importDocument('session-stable', attachment)
    await store.importDocument('session-stable', attachment)

    const firstKey = importDocument.mock.calls[0]?.[0].idempotencyKey
    const secondKey = importDocument.mock.calls[1]?.[0].idempotencyKey
    expect(firstKey).toMatch(/^import-/)
    expect(secondKey).toBe(firstKey)
  })

  it('single-flights concurrent imports of the same immutable source', async () => {
    let completeImport!: (value: Awaited<ReturnType<
      WorkbenchResourceProvider['importDocument']
    >>) => void
    const imported = {
      document: { documentId: 'doc-single-flight' },
      revision: { revisionId: 'rev-single-flight' },
      binding: { bindingId: 'bind-single-flight' },
      receipt: { status: 'applied' },
    } as Awaited<ReturnType<WorkbenchResourceProvider['importDocument']>>
    const importDocument = vi.fn(() => new Promise<typeof imported>((resolve) => {
      completeImport = resolve
    }))
    const list = vi.fn(async () => ({ resources: [attachment], totalCount: 1 }))
    const provider = {
      available: () => true,
      list,
      get: vi.fn(),
      importDocument,
      publishDocument: vi.fn(),
    } as unknown as WorkbenchResourceProvider
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)

    const first = store.importDocument('session-single-flight', attachment)
    const second = store.importDocument('session-single-flight', attachment)

    expect(importDocument).toHaveBeenCalledTimes(1)
    completeImport(imported)
    await expect(Promise.all([first, second])).resolves.toEqual([imported, imported])
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('keeps distinct import identities in separate flights', async () => {
    const imported = {
      document: { documentId: 'doc-distinct' },
      revision: { revisionId: 'rev-distinct' },
      binding: { bindingId: 'bind-distinct' },
      receipt: { status: 'applied' },
    } as Awaited<ReturnType<WorkbenchResourceProvider['importDocument']>>
    const completions: Array<(value: typeof imported) => void> = []
    const importDocument = vi.fn(() => new Promise<typeof imported>((resolve) => {
      completions.push(resolve)
    }))
    const provider = {
      available: () => true,
      list: vi.fn(async () => ({ resources: [attachment], totalCount: 1 })),
      get: vi.fn(),
      importDocument,
      publishDocument: vi.fn(),
    } as unknown as WorkbenchResourceProvider
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)

    const requests = [
      store.importDocument('session-a', attachment),
      store.importDocument('session-b', attachment),
      store.importDocument('session-a', {
        ...attachment,
        resource: { type: 'attachment', id: 'att-b' },
      }),
      store.importDocument('session-a', { ...attachment, sha256: 'b'.repeat(64) }),
      store.importDocument('session-a', { ...attachment, name: 'other.html' }),
    ]

    expect(importDocument).toHaveBeenCalledTimes(5)
    completions.forEach(complete => complete(imported))
    await expect(Promise.all(requests)).resolves.toHaveLength(5)
  })

  it('clears a failed import flight so the same source can be retried', async () => {
    const imported = {
      document: { documentId: 'doc-retry' },
      revision: { revisionId: 'rev-retry' },
      binding: { bindingId: 'bind-retry' },
      receipt: { status: 'applied' },
    } as Awaited<ReturnType<WorkbenchResourceProvider['importDocument']>>
    const importDocument = vi.fn<WorkbenchResourceProvider['importDocument']>()
      .mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValueOnce(imported)
    const list = vi.fn(async () => ({ resources: [attachment], totalCount: 1 }))
    const provider = {
      available: () => true,
      list,
      get: vi.fn(),
      importDocument,
      publishDocument: vi.fn(),
    } as unknown as WorkbenchResourceProvider
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)

    const first = store.importDocument('session-import-retry', attachment)
    const second = store.importDocument('session-import-retry', attachment)
    const failed = await Promise.allSettled([first, second])

    expect(failed.map(result => result.status)).toEqual(['rejected', 'rejected'])
    expect(importDocument).toHaveBeenCalledTimes(1)
    await expect(store.importDocument('session-import-retry', attachment)).resolves.toEqual(imported)
    expect(importDocument).toHaveBeenCalledTimes(2)
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('reuses an uncertain publish key and retires it only after an applied receipt', async () => {
    const appliedPublication = (suffix: 'a' | 'b') => ({
      deliverable: { id: `deliverable-${suffix}` },
      publication: {
        publicationId: `publication-${suffix}`,
        documentId: 'doc-a',
        revisionId: 'rev-a',
        artifactId: `deliverable-${suffix}`,
      },
      receipt: {
        attemptId: `attempt-${suffix}`,
        requestId: `publish-${suffix}`,
        idempotencyKey: `publish-${suffix}`,
        status: 'applied' as const,
        replayed: false,
      },
    })
    const publishDocument = vi.fn<WorkbenchResourceProvider['publishDocument']>()
      .mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValueOnce(appliedPublication('a'))
      .mockResolvedValueOnce(appliedPublication('b'))
    const provider = {
      available: () => true,
      list: vi.fn(async () => ({ resources: [], totalCount: 0 })),
      get: vi.fn(),
      importDocument: vi.fn(),
      publishDocument,
    } as unknown as WorkbenchResourceProvider
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)

    await expect(store.publishDocument(
      'session-publish-retry',
      'doc-a',
      'rev-a',
    )).rejects.toThrow('response lost')
    await store.publishDocument('session-publish-retry', 'doc-a', 'rev-a')
    await store.publishDocument('session-publish-retry', 'doc-a', 'rev-a')

    const keys = publishDocument.mock.calls.map(call => call[0].idempotencyKey)
    expect(keys[0]).toMatch(/^publish-/)
    expect(keys[1]).toBe(keys[0])
    expect(keys[2]).not.toBe(keys[1])
  })

  it('fails closed when editing is not advertised', async () => {
    const provider = {
      available: () => true,
      list: vi.fn(),
      get: vi.fn(),
      importDocument: vi.fn(),
      publishDocument: vi.fn(),
    } as unknown as WorkbenchResourceProvider
    const store = useWorkbenchResourcesStore()
    store.setProvider(provider)

    await expect(store.importDocument('session-a', {
      ...attachment,
      capabilities: { ...attachment.capabilities, edit: false },
    })).rejects.toThrow('cannot be imported')
    expect(provider.importDocument).not.toHaveBeenCalled()
  })
})
