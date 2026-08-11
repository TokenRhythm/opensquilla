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

  it('opens generated deliverables through the typed isolated preview when available', () => {
    const typedStart = chatViewSource.indexOf('async function openDeliverableWorkbenchResource(')
    const openStart = chatViewSource.indexOf('function openArtifact(')
    const end = chatViewSource.indexOf('\nfunction closeDeliverables', openStart)
    const source = chatViewSource.slice(typedStart, end)

    expect(typedStart).toBeGreaterThan(-1)
    expect(source).toContain("createWorkbenchResourceRef('deliverable', artifactId)")
    expect(source).toContain('workbenchResourcesStore.resolve(sessionKey.value, ref)')
    expect(source).toContain('workbenchResourcesStore.preview(sessionKey.value, ref)')
    expect(source).toContain('preparedPreview: preview.preview')
    expect(source).toContain('previewLeaseEligible: false')
    expect(source).toContain('openLegacyArtifactWorkbench(artifact)')
  })

  it('refreshes the typed resource inventory when a new deliverable appears', () => {
    const start = chatViewSource.indexOf('let workbenchArtifactInventoryFingerprint')
    const end = chatViewSource.indexOf('\nfunction openLegacyArtifactWorkbench', start)
    const source = chatViewSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('watch(sessionArtifacts, artifacts => {')
    expect(source).toContain('workbenchResourcesStore.load(sessionKey.value, true)')
  })
})
