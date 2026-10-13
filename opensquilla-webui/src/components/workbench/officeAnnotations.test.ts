// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useArtifactPromptAnnotationsStore } from '@/stores/artifactPromptAnnotations'
import { pageAnnotationSnapshots, pageContextForAnnotations } from '@/types/pageContext'
import { normalizeOfficeSelection, projectOfficeSelection } from '@/types/officeSelection'
import { promptAnnotationAllowed } from '@/types/promptAnnotations'
import { createArtifactPreviewWorkbenchItem } from '@/workbench/artifactItems'
import { ARTIFACT_DOCUMENT_RPC_METHODS, createRpcArtifactDocumentProvider } from '@/adapters/gateway/artifactDocumentsV4'
import { createArtifactWorkbenchDefinitions, type ArtifactWorkbenchProviderOptions } from './artifactWorkbenchProvider'
import { artifactWorkbenchPreviewKind, OFFICE_PREVIEW_MIMES } from '@/utils/workbench/artifactPreview'

beforeEach(() => { localStorage.clear(); setActivePinia(createPinia()) })

async function harness(officeEnabled = true, nativeEnabled = false) {
  const sourceArtifact = { id: 'published-artifact', name: '中文.pptx', mime: OFFICE_PREVIEW_MIMES.pptx,
    documentId: 'document-1', sha256: 'a'.repeat(64), size: 42 }
  // Match the Gateway wire shape: immutable deliveries have no revision pin.
  // The adapter must carry the resolved revision into the rendered head.
  const provider = createRpcArtifactDocumentProvider({
    call: async <T>(method: string): Promise<T> => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.capabilities) {
        return { formats: { pptx: { preview: true, selectionContext: true, agentEdit: true } } } as T
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet) {
        return { document: { id: 'document-1', name: sourceArtifact.name, format: 'pptx',
          headRevisionId: 'revision-1', generation: 1, stateRevision: 1,
          capabilities: { preview: true, selectionContext: true, agentEdit: true } } } as T
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList) {
        return { revisions: [{ id: 'revision-1', documentId: 'document-1', generation: 1,
          artifactId: 'artifact-1', sha256: sourceArtifact.sha256, name: sourceArtifact.name,
          mime: sourceArtifact.mime, size: sourceArtifact.size, source: 'initial' }] } as T
      }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.changesList) return { changeSets: [] } as T
      throw new Error(`Unexpected RPC: ${method}`)
    },
    hasRpcMethod: () => true,
  })
  const workspace = await provider.loadWorkspace(sourceArtifact, 'session-1')
  const artifact = workspace.headArtifact
  const store = useArtifactPromptAnnotationsStore()
  const options = { baseOrigin: 'http://localhost', currentSessionId: () => 'session-1',
    platform: { id: 'web', capabilities: {}, files: {} }, artifactContent: {}, artifactPreviews: {},
    confirmRemoteResources: vi.fn(), pushToast: vi.fn(), t: (key: string) => key,
    officePromptAnnotations: officeEnabled ? store : undefined,
    promptAnnotations: nativeEnabled ? store : undefined,
    artifactDocuments: { load: async () => undefined,
      headArtifact: () => artifact, snapshot: () => ({ workspace }) },
  } as unknown as ArtifactWorkbenchProviderOptions
  const item = createArtifactPreviewWorkbenchItem({ artifact, sessionKey: 'session-1', nativeHtml: false })
  const renderState: Record<string, unknown> = {}
  const definition = createArtifactWorkbenchDefinitions(options)[0]
  const runtime = await definition.createRuntime!(item, { getRenderState: () => renderState,
    updateRenderState: patch => Object.assign(renderState, patch), isItemOpen: () => true,
    setExpanded: vi.fn(), reportError: vi.fn() })
  const selection = { format: 'pptx' as const, kind: 'element' as const, slideIndex: 0,
    shapeId: 'title-1', text: '中文标题', truncated: false }
  const select = (revisionId = artifact.revisionId) => runtime.handleComponentEvent!({ type: 'office-annotation-selected',
    payload: { selection, revisionId, sha256: artifact.sha256 } }, item)
  return { runtime, renderState, store, artifact, workspace, item, select, definition, selection }
}

describe('Office preview context', () => {
  it('cannot use the native annotation service when Office context is disabled', async () => {
    const h = await harness(false, true)
    expect(h.definition.getProps!(h.item, { active: true, hostAvailable: true, nativeSurface: false, runtimeState: {} }).officeAnnotationEnabled).toBe(false)
    await h.select()
    expect(h.store.drafts).toEqual([])
    expect(h.renderState.annotationFallback).toBeUndefined()
    await h.runtime.dispose?.('closed')
  })

  it('filters disabled native drafts before selecting an active document or blocking Office send', async () => {
    const h = await harness()
    await h.select()
    const officeId = (h.renderState.annotationFallback as { annotationId: string }).annotationId
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: officeId, body: '修改标题' } }, h.item)
    await h.store.create({ annotationId: 'native-old', sessionKey: 'session-1', documentId: 'html-1',
      resourceId: 'document:html-1', body: 'Old HTML instruction',
      selection: { selectionId: 'native-old', tagName: 'h1', elementPath: 'h1', targetRef: 'old-target' } })
    h.store.beginOverlayEdit('native-old', 'session-1')
    const officeOnly = (item: Parameters<typeof promptAnnotationAllowed>[0]) => promptAnnotationAllowed(item, { native: false, office: true })
    expect(h.store.activeDraftsForSession('session-1', officeOnly).map(item => item.annotationId)).toEqual([officeId])
    expect(h.store.sendableDraftsForSession('session-1', officeOnly).map(item => item.annotationId)).toEqual([officeId])
    expect(h.store.sendBlockedReason('session-1', officeOnly)).toBeNull()
    expect(officeOnly(h.store.annotations['native-old'])).toBe(false)
    expect(promptAnnotationAllowed({ selection: { format: 'html', kind: 'element' } } as never, { native: true, office: true })).toBe(false)
    expect(promptAnnotationAllowed(h.store.annotations[officeId], { native: true, office: false })).toBe(false)
    await h.runtime.dispose?.('closed')
  })
  it.each(['docx', 'pptx', 'xlsx'] as const)('recognizes %s with OOXML and generic MIME while keeping PDF native', format => {
    expect(artifactWorkbenchPreviewKind({ name: `test.${format}`, mime: OFFICE_PREVIEW_MIMES[format] })).toBe(format)
    expect(artifactWorkbenchPreviewKind({ name: `test.${format}`, mime: 'application/octet-stream' })).toBe(format)
    expect(artifactWorkbenchPreviewKind({ name: 'test.pdf', mime: 'application/pdf' })).toBe('pdf')
    expect(artifactWorkbenchPreviewKind({ name: 'test.doc', mime: 'application/msword' })).toBe('unsupported')
  })

  it('uses the trusted fallback, ordinary local draft, send snapshot and history without a browser target', async () => {
    const h = await harness()
    expect(h.artifact).toMatchObject({ revisionId: 'revision-1', sha256: 'a'.repeat(64) })
    expect(h.definition.getProps!(h.item, { active: true, hostAvailable: true, nativeSurface: false, runtimeState: {} }).officeAnnotationEnabled).toBe(true)
    await h.select()
    const fallback = h.renderState.annotationFallback as { annotationId: string }
    expect(fallback).toBeTruthy()
    expect(h.store.sendableDraftsForSession('session-1')).toEqual([])
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: fallback.annotationId, body: '把标题改成中文月报' } }, h.item)
    const snapshots = h.store.snapshotsForIds([fallback.annotationId])
    const context = pageContextForAnnotations(snapshots)!
    expect(context).toMatchObject({ resourceId: 'document:document-1', revisionId: 'revision-1', sha256: 'a'.repeat(64),
      annotations: [{ text: '把标题改成中文月报', selection: h.selection }] })
    expect(context).not.toHaveProperty('targetRef')
    expect(pageAnnotationSnapshots(context)[0]).toMatchObject({ selection: h.selection, revisionId: 'revision-1', sha256: 'a'.repeat(64) })
    setActivePinia(createPinia())
    expect(useArtifactPromptAnnotationsStore().snapshotsForIds([fallback.annotationId])[0].selection).toEqual(h.selection)
    await h.runtime.dispose?.('closed')
  })

  it('rejects stale selection before creating a draft and clears canceled overlays', async () => {
    const h = await harness()
    await h.select('revision-old')
    expect(h.store.drafts).toEqual([])
    await h.select()
    const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-cancel', payload: { annotationId: id } }, h.item)
    expect(h.store.drafts).toEqual([])
    expect(h.renderState.annotationFallback).toBeNull()
    await h.runtime.dispose?.('closed')
  })

  it('does not let an old acknowledgement erase a newer version or combine versions', async () => {
    const h = await harness()
    const request = { annotationId: 'one', sessionKey: 'session-1', documentId: 'document-1',
      resourceId: 'document:document-1', revisionId: 'r1', sha256: 'a'.repeat(64), officeSelection: h.selection,
      body: '修改', selection: { selectionId: 'one', tagName: 'element', elementPath: '' } }
    await h.store.create(request)
    const old = h.store.snapshotsForIds(['one'])
    await h.store.create({ ...request, revisionId: 'r2' })
    expect(h.store.acknowledgeSent(old)).toEqual([])
    expect(() => pageContextForAnnotations([...old, ...h.store.snapshotsForIds(['one'])])).toThrow()
    await h.runtime.dispose?.('closed')
  })

  it('preserves source locators, marks truncation and projects large XLSX row numbers with formulas', () => {
    const source = { story: 'body', storyInstance: 'document', path: [0, 2] }
    expect(projectOfficeSelection({ format: 'docx', kind: 'text', runs: [{ source }], text: '中文' })?.source).toEqual(source)
    expect(normalizeOfficeSelection({ format: 'docx', kind: 'text', paragraphIds: Array(70).fill('p') })?.truncated).toBe(true)
    expect(projectOfficeSelection({ format: 'xlsx', kind: 'range', sheetIndex: 0, sheetName: '月报',
      selection: { areas: [{ kind: 'cells', top: 100001, left: 1, bottom: 100002, right: 2 }] },
      cells: [{ address: { row: 100001, col: 1 }, displayText: '3', formula: 'SUM(B1:B2)' }] }))
      .toMatchObject({ range: 'A100001:B100002', text: 'A100001: 3 [formula: SUM(B1:B2)]' })
    expect(normalizeOfficeSelection({ format: 'xlsx', kind: 'text' })).toBeNull()
  })
})
