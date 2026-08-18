import { describe, expect, it } from 'vitest'
import type { ArtifactPayload } from '@/types/rpc'
import {
  artifactFromWorkbenchItem,
  artifactsFromWorkbenchItem,
  artifactWorkbenchItemId,
  createArtifactCollectionWorkbenchItem,
  createArtifactPreviewWorkbenchItem,
  initialSectionFromWorkbenchItem,
  initialSectionRequestIdFromWorkbenchItem,
  navigationArtifactsFromWorkbenchItem,
  preparedPreviewFromWorkbenchItem,
  previewableNavigationArtifactsFromWorkbenchItem,
  requestInitialSectionForWorkbenchItem,
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

  it('uses a typed resource identity instead of filename fallbacks', () => {
    const legacy = { name: 'same.html', mime: 'text/html', download_url: '/same' }
    const attachment = createArtifactPreviewWorkbenchItem({
      artifact: legacy,
      nativeHtml: false,
      previewLeaseEligible: false,
      resourceIdentity: 'attachment:att_1',
      sessionKey: 'session-a',
    })
    const document = createArtifactPreviewWorkbenchItem({
      artifact: legacy,
      nativeHtml: false,
      resourceIdentity: 'document:doc_1',
      sessionKey: 'session-a',
    })

    expect(attachment.id).not.toBe(document.id)
    expect(attachment.payload.previewLeaseEligible).toBe(false)
  })

  it('preserves Source as an explicit initial section for direct document navigation', () => {
    const source = createArtifactPreviewWorkbenchItem({
      artifact,
      initialSection: 'source',
      nativeHtml: false,
      sessionKey: 'session-a',
    })
    const preview = createArtifactPreviewWorkbenchItem({
      artifact,
      nativeHtml: false,
      sessionKey: 'session-a',
    })

    expect(initialSectionFromWorkbenchItem(source)).toBe('source')
    expect(initialSectionFromWorkbenchItem(preview)).toBe('preview')
  })

  it('preserves repeated section requests for an already-open document', () => {
    const item = createArtifactPreviewWorkbenchItem({
      artifact,
      initialSection: 'preview',
      initialSectionRequestId: 2,
      nativeHtml: false,
      sessionKey: 'session-a',
    })

    expect(initialSectionRequestIdFromWorkbenchItem(item)).toBe(2)
  })

  it('increments section requests only for the same logical Workbench item', () => {
    const first = requestInitialSectionForWorkbenchItem(
      createArtifactPreviewWorkbenchItem({
        artifact,
        initialSection: 'preview',
        nativeHtml: false,
        sessionKey: 'session-a',
      }),
      null,
    )
    const second = requestInitialSectionForWorkbenchItem(
      createArtifactPreviewWorkbenchItem({
        artifact,
        initialSection: 'preview',
        nativeHtml: false,
        sessionKey: 'session-a',
      }),
      first,
    )
    const unrelated = requestInitialSectionForWorkbenchItem(
      createArtifactPreviewWorkbenchItem({
        artifact: { ...artifact, id: 'artifact-2' },
        initialSection: 'preview',
        nativeHtml: false,
        sessionKey: 'session-a',
      }),
      second,
    )

    expect(initialSectionRequestIdFromWorkbenchItem(first)).toBe(1)
    expect(initialSectionRequestIdFromWorkbenchItem(second)).toBe(2)
    expect(initialSectionRequestIdFromWorkbenchItem(unrelated)).toBe(1)
  })

  it('preserves only a validated opaque-offline prepared preview policy', () => {
    const item = createArtifactPreviewWorkbenchItem({
      artifact: {
        ...artifact,
        workbenchResourceType: 'attachment',
        workbenchResourceId: 'att_1',
      },
      nativeHtml: false,
      preparedPreview: {
        protocolVersion: 1,
        mode: 'isolated',
        resource: { type: 'attachment', id: 'att_1' },
        launchUrl: '/prepared/att_1',
        sandboxProfile: 'opaque-offline',
        network: false,
        adapter: { kind: 'html' },
      },
      previewLeaseEligible: false,
      resourceIdentity: 'attachment:att_1',
      sessionKey: 'session-a',
    })

    expect(preparedPreviewFromWorkbenchItem(item)).toEqual({
      protocolVersion: 1,
      mode: 'isolated',
      resource: { type: 'attachment', id: 'att_1' },
      launchUrl: '/prepared/att_1',
      sandboxProfile: 'opaque-offline',
      network: false,
      adapter: { kind: 'html' },
    })
    expect(preparedPreviewFromWorkbenchItem({
      ...item,
      payload: {
        ...item.payload,
        preparedPreview: {
          ...(item.payload.preparedPreview as Record<string, unknown>),
          network: true,
        },
      },
    })).toBeNull()
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
      slides,
    ])
  })
})
