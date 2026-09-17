import { describe, expect, it } from 'vitest'
import {
  artifactCategory,
  artifactIconName,
  artifactMime,
  canPreview,
} from './artifacts'

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

describe('artifact MIME compatibility', () => {
  it('normalizes media type parameters from older payloads', () => {
    const html = { name: 'page.bin', mime: ' Text/HTML; Charset=UTF-8 ' }
    const text = { name: 'notes.bin', mime: 'text/plain;charset=utf-8' }

    expect(artifactMime(html)).toBe('text/html')
    expect(artifactCategory(html)).toBe('document')
    expect(canPreview(html)).toBe(true)
    expect(artifactMime(text)).toBe('text/plain')
    expect(artifactCategory(text)).toBe('document')
  })
})
