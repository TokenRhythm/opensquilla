import { describe, expect, it } from 'vitest'
import { artifactCategory, canPreview } from './artifacts'

describe('image artifact compatibility', () => {
  it.each(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'ico', 'svg'])(
    'recognizes legacy .%s artifacts with a generic MIME',
    extension => {
      const artifact = {
        name: `preview.${extension}`,
        mime: 'application/octet-stream',
      }

      expect(artifactCategory(artifact)).toBe('visual')
      expect(canPreview(artifact)).toBe(true)
    },
  )

  it('does not override a specific non-image MIME from the file extension', () => {
    expect(artifactCategory({
      name: 'misleading.png',
      mime: 'text/plain',
    })).toBe('document')
  })
})
