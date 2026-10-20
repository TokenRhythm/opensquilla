import { describe, expect, it, vi } from 'vitest'
import { createWorkbenchResourceRef, type WorkbenchResource, type WorkbenchResourceRef } from '@/types/workbenchResources'
import type { ArtifactDocumentWorkspaceSnapshot } from '@/types/artifactDocuments'
import { createLegacyArtifactWorkspace } from './artifactDocumentProvider'
import { prepareCurrentDocumentPreview, resolveCurrentDocumentResource } from './currentDocumentOpening'

function resource(type: WorkbenchResourceRef['type'], id: string, documentId?: string): WorkbenchResource {
  return {
    resource: createWorkbenchResourceRef(type, id), name: 'report.docx',
    mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    capabilities: { preview: true, download: true, selectionContext: true, manualEdit: false,
      agentEdit: true, edit: true, publish: false },
    relations: documentId ? { documentId } : {},
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(finish => { resolve = finish })
  return { promise, resolve }
}

describe('resolveCurrentDocumentResource', () => {
  it('reads an explicit Document directly without creating or importing a copy', async () => {
    const current = resource('document', 'doc-one')
    const resolve = vi.fn().mockResolvedValue(current)
    expect(await resolveCurrentDocumentResource(resolve, 'session-a', current.resource)).toBe(current)
    expect(resolve).toHaveBeenCalledExactlyOnceWith('session-a', current.resource)
  })

  it('follows only server-declared lineage through read-only resource discovery', async () => {
    const delivery = resource('deliverable', 'delivered-v1', 'doc-one')
    const current = resource('document', 'doc-one')
    const resolve = vi.fn().mockResolvedValueOnce(delivery).mockResolvedValueOnce(current)
    expect(await resolveCurrentDocumentResource(resolve, 'session-a', delivery.resource)).toBe(current)
    expect(resolve.mock.calls).toEqual([
      ['session-a', delivery.resource],
      ['session-a', createWorkbenchResourceRef('document', 'doc-one')],
    ])
  })

  it('keeps same-name files with different Document identities independent', async () => {
    const originals = [
      resource('deliverable', 'original-v1', 'doc-original'),
      resource('deliverable', 'copy-v1', 'doc-copy'),
    ]
    const current = [resource('document', 'doc-original'), resource('document', 'doc-copy')]
    const resolve = vi.fn().mockResolvedValueOnce(originals[0]).mockResolvedValueOnce(current[0])
      .mockResolvedValueOnce(originals[1]).mockResolvedValueOnce(current[1])
    expect(await resolveCurrentDocumentResource(resolve, 'session-a', originals[0]!.resource)).toBe(current[0])
    expect(await resolveCurrentDocumentResource(resolve, 'session-a', originals[1]!.resource)).toBe(current[1])
    expect(resolve.mock.calls[1]?.[1]).toEqual(createWorkbenchResourceRef('document', 'doc-original'))
    expect(resolve.mock.calls[3]?.[1]).toEqual(createWorkbenchResourceRef('document', 'doc-copy'))
  })

  it('leaves an unlinked delivery unchanged and does not import it or guess by filename', async () => {
    const delivery = resource('deliverable', 'unlinked')
    const resolve = vi.fn().mockResolvedValue(delivery)
    expect(await resolveCurrentDocumentResource(resolve, 'session-a', delivery.resource)).toBe(delivery)
    expect(resolve).toHaveBeenCalledTimes(1)
  })

  it('does not silently substitute old delivery bytes when its linked Document is unavailable', async () => {
    const delivery = resource('deliverable', 'old', 'missing')
    const resolve = vi.fn().mockResolvedValueOnce(delivery).mockResolvedValueOnce(null)
    await expect(resolveCurrentDocumentResource(resolve, 'session-a', delivery.resource))
      .rejects.toMatchObject({ code: 'RESOURCE_UNSUPPORTED' })
  })

  it('does not fall back to old bytes when discovering the current Document fails', async () => {
    const delivery = resource('deliverable', 'old', 'forbidden')
    const forbidden = new Error('permission denied')
    const resolve = vi.fn().mockResolvedValueOnce(delivery).mockRejectedValueOnce(forbidden)
    await expect(resolveCurrentDocumentResource(resolve, 'session-a', delivery.resource)).rejects.toBe(forbidden)
  })

  it('discards a late first response before following its Document lineage', async () => {
    const delivery = resource('deliverable', 'old', 'doc-one')
    const pending = deferred<WorkbenchResource>()
    const resolve = vi.fn().mockReturnValue(pending.promise)
    let active = true
    const opening = resolveCurrentDocumentResource(resolve, 'session-a', delivery.resource, () => active)
    active = false
    pending.resolve(delivery)
    expect(await opening).toBeNull()
    expect(resolve).toHaveBeenCalledTimes(1)
  })

  it('discards a late current Document response after navigation changes', async () => {
    const delivery = resource('deliverable', 'old', 'doc-one')
    const pending = deferred<WorkbenchResource>()
    const resolve = vi.fn().mockResolvedValueOnce(delivery).mockReturnValueOnce(pending.promise)
    let active = true
    const opening = resolveCurrentDocumentResource(resolve, 'session-a', delivery.resource, () => active)
    await Promise.resolve()
    expect(resolve).toHaveBeenCalledTimes(2)
    active = false
    pending.resolve(resource('document', 'doc-one'))
    expect(await opening).toBeNull()
  })
})

describe('prepareCurrentDocumentPreview', () => {
  const current = { ...resource('document', 'doc-one', 'doc-one'),
    sourcePath: 'preview-gallery/report.docx',
    relations: { documentId: 'doc-one', headRevisionId: 'rev-two', headArtifactId: 'art-two' } }
  function snapshot(revisionId: string): ArtifactDocumentWorkspaceSnapshot {
    const artifact = { id: `art-${revisionId}`, documentId: 'doc-one', name: 'report.docx',
      mime: current.mime, revisionId, revision_download_url: `/revisions/${revisionId}` }
    const legacy = createLegacyArtifactWorkspace(artifact, 'session-a')
    return { key: 'session-a/doc-one', loaded: true, loading: false, stale: false, error: null,
      workspace: { ...legacy, source: 'document-api',
        document: { ...legacy.document, documentId: 'doc-one', headRevisionId: revisionId },
        headArtifact: artifact } }
  }

  it('refreshes an older cached head before reusing the current Document tab', async () => {
    let state = snapshot('rev-one')
    const documents = { snapshot: vi.fn(() => state), load: vi.fn(async () => {
      state = snapshot('rev-two')
      return state.workspace!
    }) }
    const result = await prepareCurrentDocumentPreview(current, 'session-a', documents)
    expect(documents.load).toHaveBeenCalledWith(expect.objectContaining({ documentId: 'doc-one', revisionId: 'rev-two' }),
      'session-a', { force: true })
    expect(result).toMatchObject({ documentId: 'doc-one', revisionId: 'rev-two',
      revision_download_url: '/revisions/rev-two', sourcePath: 'preview-gallery/report.docx' })
  })

  it('reuses a healthy cache only when it matches the freshly discovered head', async () => {
    const state = snapshot('rev-two')
    const documents = { snapshot: vi.fn(() => state), load: vi.fn() }
    expect(await prepareCurrentDocumentPreview(current, 'session-a', documents))
      .toMatchObject({ revisionId: 'rev-two' })
    expect(documents.load).not.toHaveBeenCalled()
  })

  it('does not silently show the preserved old cache after a refresh failure', async () => {
    let state = snapshot('rev-one')
    const documents = { snapshot: vi.fn(() => state), load: vi.fn(async () => {
      state = { ...state, stale: true, error: 'Disconnected' }
      return state.workspace!
    }) }
    await expect(prepareCurrentDocumentPreview(current, 'session-a', documents))
      .rejects.toMatchObject({ code: 'DOCUMENT_UNAVAILABLE' })
  })

  it('rejects a legacy fallback instead of presenting it as the latest Document', async () => {
    let state = snapshot('rev-one')
    const documents = { snapshot: vi.fn(() => state), load: vi.fn(async () => {
      state = { ...snapshot('rev-two'), workspace: createLegacyArtifactWorkspace({ id: 'old', name: 'report.docx' }, 'session-a') }
      return state.workspace!
    }) }
    await expect(prepareCurrentDocumentPreview(current, 'session-a', documents))
      .rejects.toMatchObject({ code: 'DOCUMENT_UNAVAILABLE' })
  })

  it('discards a refreshed preview when its original open request is no longer current', async () => {
    const state = snapshot('rev-one')
    const completion = deferred<NonNullable<ArtifactDocumentWorkspaceSnapshot['workspace']>>()
    const documents = { snapshot: vi.fn(() => state), load: vi.fn(() => completion.promise) }
    let active = true
    const preparing = prepareCurrentDocumentPreview(current, 'session-a', documents, () => active)
    active = false
    completion.resolve(snapshot('rev-two').workspace!)
    expect(await preparing).toBeNull()
  })
})
