import { describe, expect, it } from 'vitest'
import chatViewSource from './ChatView.vue?raw'

describe('ChatView artifact preview routing', () => {
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
    expect(source).toContain('artifactDownloadUrl(artifact, window.location.origin')
    expect(source).toContain("downloadBlob(blob, artifact.name || 'artifact')")
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

    expect(explicitOpenCalls).toHaveLength(8)
    expect(chatViewSource).toContain('function artifactPreviewItemForExplicitOpen(')
    expect(refreshSource).toContain(
      'initialSectionRequestId: initialSectionRequestIdFromWorkbenchItem(item)',
    )
    expect(refreshSource).not.toContain('artifactPreviewItemForExplicitOpen(')
  })
})
