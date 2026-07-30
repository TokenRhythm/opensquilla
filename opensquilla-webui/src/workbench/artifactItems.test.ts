import { describe, expect, it } from 'vitest'
import type { ArtifactPayload } from '@/types/rpc'
import {
  artifactFromWorkbenchItem,
  artifactsFromWorkbenchItem,
  artifactWorkbenchItemId,
  createArtifactCollectionWorkbenchItem,
  createArtifactPreviewWorkbenchItem,
  navigationArtifactsFromWorkbenchItem,
  previewableNavigationArtifactsFromWorkbenchItem,
} from './artifactItems'

const artifact: ArtifactPayload = {
  id: 'artifact-1',
  name: 'preview.html',
  mime: 'text/html',
  size: 128,
  download_url: '/api/v1/artifacts/artifact-1',
}

describe('artifact Workbench items', () => {
  it('uses stable session-scoped identities without embedding raw session keys', () => {
    const first = artifactWorkbenchItemId('agent:main:webchat:private', artifact)
    const second = artifactWorkbenchItemId('agent:main:webchat:private', { ...artifact })

    expect(second).toBe(first)
    expect(first).not.toContain('agent:main')
    expect(first).not.toContain('artifact-1')
  })

  it('does not alias distinct legacy artifacts that collided under the old 32-bit key', () => {
    const first = artifactWorkbenchItemId('session-a', {
      name: 'lgwql07zsrk20078.html',
    })
    const second = artifactWorkbenchItemId('session-a', {
      name: 'aimrulzrq4569835.html',
    })

    expect(first).not.toBe(second)
    expect(first.length).toBeLessThanOrEqual(128)
    expect(second.length).toBeLessThanOrEqual(128)
  })

  it('selects the native host only for HTML when the capability is available', () => {
    const html = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const image = createArtifactPreviewWorkbenchItem({
      artifact: { ...artifact, name: 'preview.png', mime: 'image/png' },
      navigationArtifacts: [artifact],
      nativeHtml: true,
      sessionKey: 'session-a',
    })
    const webHtml = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: false,
      sessionKey: 'session-a',
    })

    expect(html.hostKind).toBe('native-webcontents')
    expect(html.retention).toBe('keep-alive')
    expect(image.hostKind).toBe('dom')
    expect(image.retention).toBe('dispose-on-suspend')
    expect(webHtml.retention).toBe('dispose-on-suspend')
    expect(artifactFromWorkbenchItem(html)).toEqual(artifact)
    expect(navigationArtifactsFromWorkbenchItem(image)).toEqual([artifact])
  })

  it('creates one stable session collection containing every artifact', () => {
    const second = { ...artifact, id: 'artifact-2', name: 'notes.txt' }
    const collection = createArtifactCollectionWorkbenchItem({
      artifacts: [artifact, second],
      sessionKey: 'session-a',
      title: 'Deliverables (2)',
    })

    expect(collection.kind).toBe('artifact-collection')
    expect(collection.id).not.toContain('session-a')
    expect(collection.title).toBe('Deliverables (2)')
    expect(artifactsFromWorkbenchItem(collection)).toEqual([artifact, second])
  })

  it('keeps every deliverable in the payload but only documents in Workbench navigation', () => {
    const pdf = {
      ...artifact,
      id: 'artifact-pdf',
      name: 'report.pdf',
      mime: 'application/pdf',
    }
    const image = {
      ...artifact,
      id: 'artifact-image',
      name: 'poster.png',
      mime: 'image/png',
    }
    const slides = {
      ...artifact,
      id: 'artifact-slides',
      name: 'slides.pptx',
      mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    }
    const navigationArtifacts = [artifact, pdf, image, slides]
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      navigationArtifacts,
      nativeHtml: false,
      sessionKey: 'session-a',
    })

    expect(navigationArtifactsFromWorkbenchItem(item)).toEqual(navigationArtifacts)
    expect(previewableNavigationArtifactsFromWorkbenchItem(item)).toEqual([
      artifact,
      pdf,
    ])
  })
})
