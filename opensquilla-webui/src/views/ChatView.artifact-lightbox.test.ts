import { describe, expect, it } from 'vitest'
import chatViewSource from './ChatView.vue?raw'
import workbenchSource from '@/components/workbench/AppWorkbench.vue?raw'

describe('ChatView artifact preview routing', () => {
  it('keeps internal Documents reachable through the existing resource button', () => {
    const countStart = chatViewSource.indexOf('const headerDeliverableCount = computed(')
    const countEnd = chatViewSource.indexOf('\nconst attachmentWorkbenchResources', countStart)
    const countSource = chatViewSource.slice(countStart, countEnd)
    const openStart = chatViewSource.indexOf('async function openDeliverables()')
    const openEnd = chatViewSource.indexOf('\nfunction focusInlineDeliverable', openStart)
    const openSource = chatViewSource.slice(openStart, openEnd)

    expect(countStart).toBeGreaterThan(-1)
    expect(countSource).toContain('workbenchResourcesStore.navigationResources(sessionKey.value).length')
    expect(countSource).toContain('sessionArtifacts.value.length')
    expect(openSource).toContain('if (headerDeliverableCount.value === 0) return')
    expect(openSource).not.toContain('if (sessionArtifacts.value.length === 0) return')
    expect(openSource).toContain('createResourceCollectionWorkbenchItem({')

    const eventStart = workbenchSource.indexOf('function onArtifactState(')
    const eventEnd = workbenchSource.indexOf('\nfunction promptAnnotationItem(', eventStart)
    const eventSource = workbenchSource.slice(eventStart, eventEnd)
    expect(eventSource).toContain('workbenchResources.load(activeSessionKey, true)')
    expect(eventSource).not.toContain('store.openItem(')
  })

  it('routes visual artifacts to the lightbox before inline or unsupported fallbacks', () => {
    const start = chatViewSource.indexOf('function openArtifact(')
    const end = chatViewSource.indexOf('\nfunction closeDeliverables', start)
    const openArtifactSource = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(openArtifactSource.indexOf("artifactCategory(artifact) === 'visual'"))
      .toBeGreaterThan(-1)
    expect(openArtifactSource.indexOf("artifactCategory(artifact) === 'visual'"))
      .toBeLessThan(openArtifactSource.indexOf('isInlineMediaArtifact(artifact)'))
    expect(openArtifactSource).toContain('artifactImageLightbox.open({')
  })

  it('resolves generated deliverables to the current head before opening Preview', () => {
    const typedStart = chatViewSource.indexOf('async function openDeliverableWorkbenchResource(')
    const openStart = chatViewSource.indexOf('function openArtifact(')
    const end = chatViewSource.indexOf('\nfunction closeDeliverables', openStart)
    const source = chatViewSource.slice(typedStart, end)

    expect(typedStart).toBeGreaterThan(-1)
    expect(source).toContain("createWorkbenchResourceRef('deliverable', artifactId)")
    expect(source).toContain('workbenchResourcesStore.resolve(sessionKey.value, ref)')
    expect(source).toContain('workbenchResourcesStore.openCurrent(sessionKey.value, resource)')
    expect(source).toContain("current?.disposition === 'document'")
    expect(source).toContain('artifactPayloadFromRevision(current.revision)')
    expect(source).toContain("initialSection: 'preview'")
    expect(source).not.toContain("initialSection: 'source'")
    expect(source).toContain('workbenchResourcesStore.preview(')
    expect(source).toContain('preparedPreview: preview.preview')
    expect(source).toContain('previewLeaseEligible: false')
    expect(source).toContain('openLegacyArtifactWorkbench(artifact)')
  })

  it('does not disguise a current-head resolution error as an old artifact preview', () => {
    const start = chatViewSource.indexOf('async function openDeliverableWorkbenchResource(')
    const end = chatViewSource.indexOf('\nfunction openArtifact(', start)
    const source = chatViewSource.slice(start, end)
    const catchStart = source.lastIndexOf('} catch (error) {')
    const catchSource = source.slice(catchStart)

    expect(catchStart).toBeGreaterThan(-1)
    expect(catchSource).toContain('classifyArtifactProductError(error)')
    expect(catchSource).toContain('classified.messageKey')
    expect(catchSource).toContain("{ tone: 'danger', duration: 9000 }")
    expect(catchSource).not.toContain('openLegacyArtifactWorkbench')
  })

  it('keeps the card download bound to the original immutable artifact', () => {
    const start = chatViewSource.indexOf('async function downloadArtifact(')
    const end = chatViewSource.indexOf('\nfunction artifactUsesDocumentWorkbench', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('artifactWorkbench.content.fetchArtifact(artifact')
    expect(source).toContain("downloadBlob(result.blob, artifact.name || 'artifact')")
    expect(source).not.toContain('openCurrent')
    expect(source).not.toContain('headArtifact')
  })

  it('opens attachment cards through the current resource before the old Gateway fallback', () => {
    const start = chatViewSource.indexOf('async function previewAttachmentResource(')
    const end = chatViewSource.indexOf('\nasync function editAttachmentResource(', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('workbenchResourcesStore.openCurrent(sessionKey.value, resource)')
    expect(source).toContain("current?.disposition === 'document'")
    expect(source).toContain('workbenchResourcesStore.importDocument(')
    expect(source.indexOf('openCurrent(sessionKey.value, resource)'))
      .toBeLessThan(source.indexOf('importDocument('))
    expect(source).toContain("initialSection: 'preview'")
    expect(source).toContain('classifyArtifactProductError(error)')
    expect(source).not.toContain('error.message')
  })

  it('refreshes the typed resource inventory when a new deliverable appears', () => {
    const start = chatViewSource.indexOf('let workbenchArtifactInventoryFingerprint')
    const end = chatViewSource.indexOf('\nfunction openLegacyArtifactWorkbench', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('watch(sessionArtifacts, artifacts => {')
    expect(source).toContain('workbenchResourcesStore.load(sessionKey.value, true)')
  })

  it('treats every user open as a fresh section request without resetting on metadata refresh', () => {
    const explicitOpenCalls = chatViewSource.match(
      /workbenchStore\.openItem\(artifactPreviewItemForExplicitOpen\(/g,
    ) || []
    const refreshStart = chatViewSource.indexOf('watch(sessionArtifacts, artifacts => {')
    const refreshEnd = chatViewSource.indexOf('\nfunction openLegacyArtifactWorkbench', refreshStart)
    const refreshSource = chatViewSource.slice(refreshStart, refreshEnd)

    expect(explicitOpenCalls).toHaveLength(9)
    expect(chatViewSource).toContain('function artifactPreviewItemForExplicitOpen(')
    expect(refreshSource).toContain(
      'initialSectionRequestId: initialSectionRequestIdFromWorkbenchItem(item)',
    )
    expect(refreshSource).not.toContain('artifactPreviewItemForExplicitOpen(')
  })

  it('resolves message site pages read-only and keeps their open target on the same Document item', () => {
    const start = chatViewSource.indexOf('async function resolveWorkspacePreviewResource(')
    const end = chatViewSource.indexOf('\nfunction openArtifact(', start)
    const source = chatViewSource.slice(start, end)
    expect(source).not.toContain('workbenchResourcesStore.find(key, ref)')
    expect(source).toContain('workbenchResourcesStore.resolve(key, ref)')
    expect(source).toContain('if (previewPagePath) artifact.previewPagePath = previewPagePath')
    expect(source).toContain('resourceIdentity: workbenchResourceKey(current.resource.resource)')
    expect(source).toContain('artifactPreviewItemForExplicitOpen({')
    expect(source).not.toContain('publishDocument')
    expect(chatViewSource).toContain(':resolve-workspace-preview-resource="resolveWorkspacePreviewResource"')
    expect(chatViewSource).toContain("typeof artifact.previewPagePath === 'string' ? artifact.previewPagePath : undefined")
  })
})
