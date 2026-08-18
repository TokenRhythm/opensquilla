import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ArtifactDocumentWorkspace } from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import {
  createLegacyArtifactWorkspace,
  type ArtifactDocumentProvider,
} from '@/workbench/artifactDocumentProvider'
import {
  artifactDocumentWorkspaceKey,
  useArtifactDocumentsStore,
} from './artifactDocuments'

const artifact: ArtifactPayload = {
  id: 'artifact-1',
  name: 'report.docx',
  mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  download_url: '/api/v1/artifacts/artifact-1',
}

function providerWithLoad(
  loadWorkspace: ArtifactDocumentProvider['loadWorkspace'],
): ArtifactDocumentProvider {
  return {
    loadWorkspace,
    getCapabilities: vi.fn(),
    listDocuments: vi.fn(),
    getDocument: vi.fn(),
    listRevisions: vi.fn(),
    listChangeSets: vi.fn(),
    getChangeSet: vi.fn(),
    openDocument: vi.fn(),
    closeDocument: vi.fn(),
    renameDocument: vi.fn(),
    restoreRevision: vi.fn(),
    revertChangeSet: vi.fn(),
    readSource: vi.fn(),
    patchSource: vi.fn(),
  }
}

function editableWorkspace(): ArtifactDocumentWorkspace {
  const legacy = createLegacyArtifactWorkspace(artifact, 'session-a')
  const documentId = 'document-1'
  const headRevisionId = 'revision-head'
  const head = {
    ...legacy.revisions[0]!,
    revisionId: headRevisionId,
    documentId,
    artifactId: 'artifact-head',
    downloadUrl: `/api/v1/artifact-documents/${documentId}`,
  }
  return {
    ...legacy,
    source: 'document-api',
    headArtifact: {
      ...artifact,
      id: 'artifact-head',
      download_url: `/api/v1/artifact-documents/${documentId}`,
    },
    document: {
      ...legacy.document,
      documentId,
      headRevisionId,
      capabilities: {
        ...legacy.document.capabilities,
        revisions: true,
        changeSets: true,
        comments: true,
      },
    },
    revisions: [
      head,
      {
        ...head,
        revisionId: 'revision-old',
        generation: 0,
        artifactId: 'artifact-old',
        downloadUrl: '/api/v1/artifact-documents/document-1?revisionId=revision-old',
      },
    ],
    changeSets: [{
      changeSetId: 'change-applied',
      documentId,
      baseRevisionId: 'revision-old',
      turnId: 'turn-1',
      summary: 'Change heading',
      status: 'applied' as const,
      operations: [{ op: 'replace' }],
      candidateArtifact: null,
      validation: { ok: true },
      stateRevision: 2,
      createdByKind: 'agent',
      createdById: 'main',
      appliedRevisionId: headRevisionId,
      createdAt: null,
      updatedAt: null,
      schemaVersion: 1,
    }],
  }
}

beforeEach(() => setActivePinia(createPinia()))

describe('artifact documents store', () => {
  it('provides a safe download-only snapshot before document RPC is configured', async () => {
    const store = useArtifactDocumentsStore()

    const workspace = await store.load(artifact, 'session-a')

    expect(workspace.source).toBe('legacy-artifact')
    expect(workspace.document.capabilities).toMatchObject({
      download: true,
      preview: false,
      edit: false,
    })
    expect(store.snapshot(artifact, 'session-a')).toMatchObject({
      loaded: true,
      loading: false,
      workspace: { source: 'legacy-artifact' },
    })
  })

  it('caches a loaded workspace and refreshes it only when requested', async () => {
    const first = createLegacyArtifactWorkspace(artifact, 'session-a')
    const second: ArtifactDocumentWorkspace = {
      ...first,
      headArtifact: { ...artifact, id: 'artifact-2' },
    }
    const loadWorkspace = vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second)
    const store = useArtifactDocumentsStore()
    store.setProvider(providerWithLoad(loadWorkspace))

    await store.load(artifact, 'session-a')
    await store.load(artifact, 'session-a')
    expect(loadWorkspace).toHaveBeenCalledOnce()

    await store.refresh(artifact, 'session-a')
    expect(loadWorkspace).toHaveBeenCalledTimes(2)
    expect(store.headArtifact(artifact, 'session-a').id).toBe('artifact-2')
  })

  it('retires a stale request before publishing a forced refresh', async () => {
    let resolveFirst!: (workspace: ArtifactDocumentWorkspace) => void
    const firstRequest = new Promise<ArtifactDocumentWorkspace>(resolve => {
      resolveFirst = resolve
    })
    const oldWorkspace = createLegacyArtifactWorkspace(artifact, 'session-a')
    const freshWorkspace: ArtifactDocumentWorkspace = {
      ...oldWorkspace,
      headArtifact: { ...artifact, id: 'artifact-fresh' },
    }
    const loadWorkspace = vi.fn()
      .mockImplementationOnce(() => firstRequest)
      .mockResolvedValueOnce(freshWorkspace)
    const store = useArtifactDocumentsStore()
    store.setProvider(providerWithLoad(loadWorkspace))

    const stale = store.load(artifact, 'session-a')
    const fresh = store.refresh(artifact, 'session-a')
    await fresh
    resolveFirst(oldWorkspace)
    await stale

    expect(store.headArtifact(artifact, 'session-a').id).toBe('artifact-fresh')
  })

  it('falls back to the original artifact when a provider fails', async () => {
    const store = useArtifactDocumentsStore()
    store.setProvider(providerWithLoad(async () => {
      throw new Error('document service unavailable')
    }))

    await expect(store.load(artifact, 'session-a')).resolves.toMatchObject({
      source: 'legacy-artifact',
    })
    expect(store.snapshot(artifact, 'session-a')).toMatchObject({
      loaded: true,
      stale: true,
      error: 'The operation could not be completed. Try again.',
      workspace: { headArtifact: artifact },
    })
  })

  it('keeps the last-known adopted head stale after a transient refresh failure', async () => {
    const adopted = editableWorkspace()
    const loadWorkspace = vi.fn()
      .mockResolvedValueOnce(adopted)
      .mockRejectedValueOnce(new Error('document refresh timed out'))
      .mockResolvedValueOnce({
        ...adopted,
        document: {
          ...adopted.document,
          headRevisionId: 'revision-next',
          generation: adopted.document.generation + 1,
        },
        headArtifact: {
          ...adopted.headArtifact,
          id: 'artifact-next',
        },
      })
    const store = useArtifactDocumentsStore()
    store.setProvider(providerWithLoad(loadWorkspace))

    await store.load(artifact, 'session-a')
    const staleWorkspace = await store.refresh(artifact, 'session-a')

    expect(staleWorkspace).toEqual(adopted)
    expect(store.headArtifact(artifact, 'session-a')).toEqual(adopted.headArtifact)
    expect(store.snapshot(artifact, 'session-a')).toMatchObject({
      loaded: true,
      loading: false,
      stale: true,
      error: 'The operation could not be completed. Try again.',
      workspace: {
        source: 'document-api',
        document: { documentId: 'document-1', headRevisionId: 'revision-head' },
        headArtifact: {
          id: 'artifact-head',
          download_url: '/api/v1/artifact-documents/document-1',
        },
      },
    })

    const refreshed = await store.refresh(artifact, 'session-a')
    expect(refreshed.headArtifact.id).toBe('artifact-next')
    expect(store.snapshot(artifact, 'session-a')).toMatchObject({
      stale: false,
      error: null,
      workspace: { headArtifact: { id: 'artifact-next' } },
    })
  })

  it('does not downgrade an adopted workspace when a provider returns legacy fallback', async () => {
    const adopted = editableWorkspace()
    const loadWorkspace = vi.fn()
      .mockResolvedValueOnce(adopted)
      .mockResolvedValueOnce(createLegacyArtifactWorkspace(artifact, 'session-a'))
    const store = useArtifactDocumentsStore()
    store.setProvider(providerWithLoad(loadWorkspace))

    await store.load(artifact, 'session-a')
    const refreshed = await store.refresh(artifact, 'session-a')

    expect(refreshed).toEqual(adopted)
    expect(store.headArtifact(artifact, 'session-a').download_url)
      .toBe('/api/v1/artifact-documents/document-1')
    expect(store.snapshot(artifact, 'session-a')).toMatchObject({
      stale: true,
      error: 'This page is temporarily unavailable. Try again.',
      workspace: { source: 'document-api' },
    })
  })

  it('clears only the requested session and aborts its in-flight request', async () => {
    const signals: AbortSignal[] = []
    const store = useArtifactDocumentsStore()
    store.setProvider(providerWithLoad((_artifact, _sessionKey, signal) => {
      if (signal) signals.push(signal)
      return new Promise(() => undefined)
    }))

    void store.load(artifact, 'session-a')
    expect(store.snapshots[artifactDocumentWorkspaceKey(artifact, 'session-a')]).toBeTruthy()
    store.clearSession('session-a')

    expect(signals[0]?.aborted).toBe(true)
    expect(store.snapshot(artifact, 'session-a').loaded).toBe(false)
  })

  it('derives mutation identity and CAS fields from the loaded workspace then refreshes', async () => {
    const workspace = editableWorkspace()
    const provider = providerWithLoad(vi.fn().mockResolvedValue(workspace))
    vi.mocked(provider.restoreRevision).mockResolvedValue(workspace.revisions[1]!)
    vi.mocked(provider.revertChangeSet).mockResolvedValue(workspace.changeSets[0]!)
    const store = useArtifactDocumentsStore()
    store.setProvider(provider)
    await store.load(artifact, 'session-a')

    await store.restoreRevision(artifact, 'session-a', 'revision-old')
    expect(provider.restoreRevision).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      documentId: workspace.document.documentId,
      revisionId: 'revision-old',
      expectedHeadRevisionId: workspace.document.headRevisionId,
      expectedStateRevision: workspace.document.stateRevision,
      clientRequestId: expect.stringMatching(/^document-restore-/),
    })

    await store.revertChangeSet(artifact, 'session-a', 'change-applied')
    expect(provider.revertChangeSet).toHaveBeenCalledWith(expect.objectContaining({
      documentId: workspace.document.documentId,
      changeSetId: 'change-applied',
      expectedHeadRevisionId: workspace.document.headRevisionId,
      expectedStateRevision: workspace.document.stateRevision,
      clientRequestId: expect.stringMatching(/^document-revert-/),
    }))

    expect(vi.mocked(provider.restoreRevision).mock.calls[0]?.[0].clientRequestId)
      .not.toBe(vi.mocked(provider.revertChangeSet).mock.calls[0]?.[0].clientRequestId)

    expect(provider.loadWorkspace).toHaveBeenCalledTimes(3)
  })

  it('keeps restore and revert request identities stable across ambiguous retries', async () => {
    const workspace = editableWorkspace()
    const provider = providerWithLoad(vi.fn().mockResolvedValue(workspace))
    const responseLost = () => Object.assign(new Error('response lost'), {
      code: 'RPC_TRANSPORT_ERROR',
      accepted: null,
    })
    vi.mocked(provider.restoreRevision).mockRejectedValue(responseLost())
    vi.mocked(provider.revertChangeSet).mockRejectedValue(responseLost())
    const store = useArtifactDocumentsStore()
    store.setProvider(provider)
    await store.load(artifact, 'session-a')

    await expect(store.restoreRevision(artifact, 'session-a', 'revision-old'))
      .rejects.toThrow('cannot be confirmed')
    await expect(store.restoreRevision(artifact, 'session-a', 'revision-old'))
      .rejects.toThrow('cannot be confirmed')
    await expect(store.revertChangeSet(artifact, 'session-a', 'change-applied'))
      .rejects.toThrow('cannot be confirmed')
    await expect(store.revertChangeSet(artifact, 'session-a', 'change-applied'))
      .rejects.toThrow('cannot be confirmed')

    const restoreIds = vi.mocked(provider.restoreRevision).mock.calls
      .map(call => String(call[0].clientRequestId))
    const revertIds = vi.mocked(provider.revertChangeSet).mock.calls
      .map(call => String(call[0].clientRequestId))
    expect(restoreIds).toHaveLength(2)
    expect(new Set(restoreIds)).toEqual(new Set([restoreIds[0]]))
    expect(restoreIds[0]).toMatch(/^document-restore-/)
    expect(revertIds).toHaveLength(2)
    expect(new Set(revertIds)).toEqual(new Set([revertIds[0]]))
    expect(revertIds[0]).toMatch(/^document-revert-/)
    expect(revertIds[0]).not.toBe(restoreIds[0])
  })

  it('accepts a durable applied restore resolution without replaying the write', async () => {
    const workspace = editableWorkspace()
    const responseLost = Object.assign(new Error('private transport detail'), {
      code: 'RPC_TRANSPORT_ERROR',
      accepted: null,
    })
    const provider = providerWithLoad(vi.fn().mockResolvedValue(workspace))
    vi.mocked(provider.restoreRevision).mockRejectedValue(responseLost)
    provider.resolveMutation = vi.fn(async () => ({
      status: 'applied' as const,
      retryAfterMs: null,
      result: {
        documentId: workspace.document.documentId,
        revisionId: 'revision-old',
        sha256: 'a'.repeat(64),
        stateRevision: workspace.document.stateRevision + 1,
      },
    }))
    const store = useArtifactDocumentsStore()
    store.setProvider(provider)
    await store.load(artifact, 'session-a')

    await expect(store.restoreRevision(artifact, 'session-a', 'revision-old'))
      .resolves.toEqual(workspace)
    expect(provider.restoreRevision).toHaveBeenCalledTimes(1)
    expect(provider.resolveMutation).toHaveBeenCalledTimes(1)
  })

  it('releases a not-applied restore identity before the next retry', async () => {
    const workspace = editableWorkspace()
    const responseLost = Object.assign(new Error('private transport detail'), {
      code: 'RPC_TRANSPORT_ERROR',
      accepted: null,
    })
    const provider = providerWithLoad(vi.fn().mockResolvedValue(workspace))
    vi.mocked(provider.restoreRevision)
      .mockRejectedValueOnce(responseLost)
      .mockResolvedValueOnce(workspace.revisions[1]!)
    provider.resolveMutation = vi.fn(async () => ({
      status: 'not_applied' as const,
      retryAfterMs: null,
      result: null,
    }))
    const store = useArtifactDocumentsStore()
    store.setProvider(provider)
    await store.load(artifact, 'session-a')

    await expect(store.restoreRevision(artifact, 'session-a', 'revision-old'))
      .rejects.toMatchObject({ code: 'MUTATION_NOT_APPLIED' })
    await expect(store.restoreRevision(artifact, 'session-a', 'revision-old'))
      .resolves.toEqual(workspace)

    const requestIds = vi.mocked(provider.restoreRevision).mock.calls
      .map(call => String(call[0].clientRequestId))
    expect(requestIds).toHaveLength(2)
    expect(requestIds[1]).not.toBe(requestIds[0])
  })

  it('fails closed for stale or unscoped review mutations', async () => {
    const workspace = editableWorkspace()
    const provider = providerWithLoad(vi.fn().mockResolvedValue(workspace))
    const store = useArtifactDocumentsStore()
    store.setProvider(provider)
    await store.load(artifact, 'session-a')

    await expect(store.restoreRevision(artifact, 'session-a', 'unknown-revision'))
      .rejects.toThrow('unavailable')
    await expect(store.revertChangeSet(artifact, 'session-a', 'unknown-change'))
      .rejects.toThrow('unavailable')
    expect(provider.restoreRevision).not.toHaveBeenCalled()
    expect(provider.revertChangeSet).not.toHaveBeenCalled()
  })
})
