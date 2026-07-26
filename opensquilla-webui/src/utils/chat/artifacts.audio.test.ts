import { describe, expect, it } from 'vitest'
import { artifactCategory, artifactIconName, canPreview } from './artifacts'

describe('audio artifact classification', () => {
  it('classifies explicit audio MIME types', () => {
    const artifact = { name: 'speech.bin', mime: 'audio/mpeg' }
    expect(artifactCategory(artifact)).toBe('audio')
    expect(artifactIconName(artifact)).toBe('music')
    expect(canPreview(artifact)).toBe(false)
  })

  it('uses safe audio extensions only for generic MIME types', () => {
    expect(artifactCategory({ name: 'speech.ogg', mime: 'application/octet-stream' })).toBe('audio')
    expect(artifactCategory({ name: 'speech.m4a' })).toBe('audio')
    expect(artifactCategory({ name: 'speech.ogg', mime: 'text/plain' })).toBe('document')
  })
})
