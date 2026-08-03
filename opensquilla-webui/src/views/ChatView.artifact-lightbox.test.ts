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
})
