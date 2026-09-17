import { describe, expect, it } from 'vitest'
import {
  artifactCategory,
  artifactIconName,
  canPreview,
  isInlineMediaArtifact,
} from './artifacts'

describe('video artifact classification', () => {
  it.each(['video/mp4', 'video/webm', 'video/quicktime'])(
    'classifies explicit %s MIME types as inline video',
    mime => {
      const artifact = { name: 'clip.bin', mime }

      expect(artifactCategory(artifact)).toBe('video')
      expect(artifactIconName(artifact)).toBe('video')
      expect(canPreview(artifact)).toBe(false)
      expect(isInlineMediaArtifact(artifact)).toBe(true)
    },
  )

  it.each(['avi', 'm4v', 'mkv', 'mov', 'mp4', 'ogv', 'webm'])(
    'recognizes a legacy .%s artifact only when its MIME is generic',
    extension => {
      expect(artifactCategory({
        name: `clip.${extension}`,
        mime: 'application/octet-stream',
      })).toBe('video')
      expect(artifactCategory({ name: `clip.${extension}` })).toBe('video')
    },
  )

  it('does not override a specific non-video MIME from the extension', () => {
    expect(artifactCategory({
      name: 'misleading.mp4',
      mime: 'text/plain',
    })).toBe('document')
  })

  it('identifies audio and video as inline media without including other artifacts', () => {
    expect(isInlineMediaArtifact({ name: 'answer.mp3', mime: 'audio/mpeg' })).toBe(true)
    expect(isInlineMediaArtifact({ name: 'answer.webm', mime: 'video/webm' })).toBe(true)
    expect(isInlineMediaArtifact({ name: 'report.pdf', mime: 'application/pdf' })).toBe(false)
  })
})
