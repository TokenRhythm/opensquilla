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
  const annotationActions = {
    ...store,
    get: (annotationId: string) => store.annotations[annotationId],
    list: (sessionKey: string, documentId: string) => store.draftsForSession(sessionKey)
      .filter(draft => draft.documentId === documentId),
    update: (annotationId: string, body: string) => store.update(annotationId, body),
    create: (request: Parameters<typeof store.create>[0]) => store.create(request),
  }
  const options = { baseOrigin: 'http://localhost', currentSessionId: () => 'session-1',
    platform: { id: 'web', capabilities: {}, files: {} }, artifactContent: {}, artifactPreviews: {},
    confirmRemoteResources: vi.fn(), pushToast: vi.fn(), t: (key: string) => key,
    officePromptAnnotations: officeEnabled ? annotationActions : undefined,
    promptAnnotations: nativeEnabled ? annotationActions : undefined,
    artifactDocuments: { load: async () => undefined,
      headArtifact: () => artifact, snapshot: () => ({ workspace }) },
  } as unknown as ArtifactWorkbenchProviderOptions
  const item = createArtifactPreviewWorkbenchItem({ artifact, sessionKey: 'session-1', nativeHtml: false,
    resourceIdentity: 'document:document-1' })
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

  it('records card and summary actions as unsent composer drafts', async () => {
    const h = await harness()
    await h.select()
    const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: id, body: '只记录，不执行' } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-summary',
      payload: { type: 'submit' } }, h.item)
    expect(h.store.annotations[id]).toMatchObject({ body: '只记录，不执行', status: 'draft' })
    expect(h.store.isQueued(id)).toBe(false)
    expect(h.store.sendableDraftsForSession('session-1').map(draft => draft.annotationId)).toEqual([id])
    expect(h.store.sendBlockedReason('session-1')).toBeNull()
    expect(h.renderState.annotationFallback).toBeNull()
    await h.runtime.dispose?.('closed')
  })

  it('restores the original text when canceling an existing draft edit', async () => {
    const h = await harness()
    await h.select()
    const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: id, body: '原有修改建议' } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-summary',
      payload: { type: 'edit', annotationId: id } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-update',
      payload: { annotationId: id, body: '本次临时修改' } }, h.item)
    expect(h.store.annotations[id].body).toBe('本次临时修改')
    expect(h.store.sendBlockedReason('session-1')).toBe('editing')
    // Reopening the same item must not replace its cancel checkpoint.
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-summary',
      payload: { type: 'edit', annotationId: id } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-cancel',
      payload: { annotationId: id, body: '本次临时修改' } }, h.item)
    expect(h.store.annotations[id].body).toBe('原有修改建议')
    expect(h.store.sendBlockedReason('session-1')).toBeNull()
    expect(h.renderState.annotationFallback).toBeNull()
    await h.runtime.dispose?.('closed')
  })

  it('preserves a failed record and prevents closing until the latest body is saved', async () => {
    const h = await harness()
    await h.select()
    const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
    const update = vi.spyOn(h.store, 'update').mockRejectedValue(new Error('Storage unavailable'))
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: id, body: '存储恢复后保留这段正文' } }, h.item)
    expect(h.renderState.annotationFallback).toMatchObject({ annotationId: id })
    expect(h.renderState.annotationError).toBeTruthy()
    expect(h.store.sendBlockedReason('session-1')).toBe('editing')
    expect(await h.runtime.beforeClose?.()).toBe(false)
    update.mockRestore()
    expect(await h.runtime.beforeClose?.()).toBe(true)
    expect(h.store.annotations[id].body).toBe('存储恢复后保留这段正文')
    expect(h.store.sendBlockedReason('session-1')).toBeNull()
    await h.runtime.dispose?.('closed')
  })

  it('finishes the previous editor before opening another draft and releases both locks', async () => {
    const h = await harness()
    const ids: string[] = []
    for (const body of ['第一条', '第二条']) {
      await h.select()
      const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
      ids.push(id)
      await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-submit',
        payload: { annotationId: id, body } }, h.item)
    }
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-summary',
      payload: { type: 'edit', annotationId: ids[0] } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-update',
      payload: { annotationId: ids[0], body: '已更新第一条' } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-summary',
      payload: { type: 'edit', annotationId: ids[1] } }, h.item)
    expect(h.store.annotations[ids[0]].body).toBe('已更新第一条')
    expect(h.renderState.annotationFallback).toMatchObject({ annotationId: ids[1] })
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-cancel',
      payload: { annotationId: ids[1] } }, h.item)
    expect(h.store.sendBlockedReason('session-1')).toBeNull()
    expect(h.store.sendableDraftsForSession('session-1')).toHaveLength(2)
    await h.runtime.dispose?.('closed')
  })

  it('saves an Office editor before suspension so a hidden card cannot block composer send', async () => {
    const h = await harness()
    await h.select()
    const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-update',
      payload: { annotationId: id, body: '切文件时保留' } }, h.item)
    await h.runtime.suspend?.(h.item)
    expect(h.store.annotations[id].body).toBe('切文件时保留')
    expect(h.store.sendBlockedReason('session-1')).toBeNull()
    expect(h.renderState.annotationFallback).toBeNull()
    await h.runtime.dispose?.('closed')
  })

  it('ignores an acknowledgement from another document or an older draft while editing a new one', async () => {
    const h = await harness()
    await h.runtime.handleComponentEvent!({ type: 'office-annotation-mode', payload: { enabled: true } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'page-annotations-sent',
      payload: { documentId: 'other-document', draftIds: [] } }, h.item)
    expect(h.renderState.annotationMode).toBe(true)
    await h.select()
    const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-update',
      payload: { annotationId: id, body: '旧回执之后的新正文' } }, h.item)
    await h.runtime.handleComponentEvent!({ type: 'page-annotations-sent',
      payload: { documentId: 'document-1', draftIds: ['previously-sent'] } }, h.item)
    expect(h.renderState.annotationMode).toBe(true)
    expect(h.renderState.annotationFallback).toMatchObject({ annotationId: id })
    expect(h.store.annotations[id].body).toBe('旧回执之后的新正文')
    await h.runtime.dispose?.('closed')
  })

  it('does not stop an Office selection whose draft creation is pending when an old acknowledgement arrives', async () => {
    const h = await harness()
    await h.runtime.handleComponentEvent!({ type: 'office-annotation-mode', payload: { enabled: true } }, h.item)
    const originalCreate = h.store.create.bind(h.store)
    let resolveCreate!: () => void
    const gate = new Promise<void>(resolve => { resolveCreate = resolve })
    const create = vi.spyOn(h.store, 'create').mockImplementation(async request => {
      await gate
      return originalCreate(request)
    })
    const selected = h.select()
    expect(create).toHaveBeenCalledOnce()
    await h.runtime.handleComponentEvent!({ type: 'page-annotations-sent',
      payload: { documentId: 'document-1', draftIds: ['previously-sent'] } }, h.item)
    expect(h.renderState.annotationMode).toBe(true)
    resolveCreate()
    await selected
    expect(h.renderState.annotationFallback).toBeTruthy()
    create.mockRestore()
    await h.runtime.dispose?.('closed')
  })

  it('ignores late locate results for the previous card and accepts only the current request', async () => {
    const h = await harness()
    const ids: string[] = []
    for (const body of ['第一条', '第二条']) {
      await h.select()
      const id = (h.renderState.annotationFallback as { annotationId: string }).annotationId
      ids.push(id)
      await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-fallback-submit',
        payload: { annotationId: id, body } }, h.item)
    }
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-summary',
      payload: { type: 'edit', annotationId: ids[0] } }, h.item)
    const oldRequest = h.renderState.officeSelectionToLocate as { requestId: string }
    await h.runtime.handleComponentEvent!({ type: 'artifact-annotation-summary',
      payload: { type: 'edit', annotationId: ids[1] } }, h.item)
    const currentRequest = h.renderState.officeSelectionToLocate as { requestId: string }
    const anchor = { rect: { left: 30, top: 40, width: 100, height: 20 }, rects: [],
      viewport: { left: 0, top: 0, width: 800, height: 600 }, visible: true }
    await h.runtime.handleComponentEvent!({ type: 'office-annotation-located',
      payload: { status: 'stale', anchor, requestId: oldRequest.requestId } }, h.item)
    expect(h.renderState.annotationFallback).toMatchObject({ annotationId: ids[1], stale: false })
    expect(h.renderState.annotationFallback).not.toHaveProperty('anchor')
    await h.runtime.handleComponentEvent!({ type: 'office-annotation-located',
      payload: { status: 'exact', anchor, requestId: currentRequest.requestId } }, h.item)
    expect(h.renderState.annotationFallback).toMatchObject({ annotationId: ids[1], stale: false, anchor })
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
