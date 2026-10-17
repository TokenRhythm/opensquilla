import { describe, expect, it } from 'vitest'
import type { WorkbenchResource } from '@/types/workbenchResources'
import { createWorkbenchResourceRef } from '@/types/workbenchResources'
import {
  artifactPayloadFromWorkbenchResource,
  canonicalWorkbenchResources,
  createResourceCollectionWorkbenchItem,
  refreshResourceCollectionWorkbenchItem,
  resourceFromPreparedPreview,
  resourceUsesNativeHtmlPreview,
  resourcesFromWorkbenchItem,
  isInternalVisualizeResource,
  workbenchResourceKey,
} from './workbenchResourceItems'

function resource(
  type: WorkbenchResource['resource']['type'],
  id: string,
  relations: WorkbenchResource['relations'] = {},
): WorkbenchResource {
  return {
    resource: createWorkbenchResourceRef(type, id),
    name: 'same.html',
    mime: 'text/html',
    size: 32,
    sha256: `${type}-sha`,
    downloadUrl: `/resources/${id}`,
    capabilities: {
      preview: true,
      download: true,
      selectionContext: false,
      manualEdit: type !== 'document',
      agentEdit: false,
      edit: type !== 'document',
      publish: type === 'document',
    },
    relations,
  }
}

describe('workbench resource items', () => {
  it('preserves parent visibility and child authority when a side inventory refreshes', () => {
    const child = 'agent:main:sidechat:child'
    const before = resource('deliverable', 'art_old')
    const after = resource('document', 'doc_new', { documentId: 'doc_new' })
    const item = createResourceCollectionWorkbenchItem({ resources: [before], sessionKey: child, title: 'Old' })
    item.scope = { type: 'session', id: 'agent:main:webchat:parent' }
    const refreshed = refreshResourceCollectionWorkbenchItem(item, [after], 'Resources')
    expect(refreshed.id).toBe(item.id)
    expect(refreshed.scope).toEqual({ type: 'session', id: 'agent:main:webchat:parent' })
    expect(refreshed.payload.sessionKey).toBe(child)
    expect(refreshed.payload.resourceCount).toBe(1)
    expect(refreshed.payload.resources).toBeUndefined()
    expect(item.payload.resources).toBeUndefined()
  })

  it('keeps same-name resources distinct by typed stable identity', () => {
    const attachment = resource('attachment', 'att_1')
    const document = resource('document', 'doc_1')
    const deliverable = resource('deliverable', 'art_1')
    const item = createResourceCollectionWorkbenchItem({
      resources: [attachment, document, deliverable],
      sessionKey: 'agent:main:webchat:test',
      title: 'Workbench',
    })

    expect(item.payload.resourceCount).toBe(3)
    expect(resourcesFromWorkbenchItem(item)).toEqual([])
    expect(new Set([
      workbenchResourceKey(attachment.resource),
      workbenchResourceKey(document.resource),
      workbenchResourceKey(deliverable.resource),
    ]).size).toBe(3)
  })

  it('projects explicit source bindings to their visible Document without name or hash merging', () => {
    const document = resource('document', 'doc_1', { documentId: 'doc_1' })
    const boundAttachment = resource('attachment', 'att_1', { documentId: 'doc_1' })
    const boundDeliverable = resource('deliverable', 'art_1', { documentId: 'doc_1' })
    const sameLookingUnbound = resource('attachment', 'att_2')
    sameLookingUnbound.sha256 = boundAttachment.sha256

    const visible = canonicalWorkbenchResources([
      boundAttachment,
      boundDeliverable,
      document,
      sameLookingUnbound,
    ])
    expect(visible).toEqual([document, sameLookingUnbound])

    const item = createResourceCollectionWorkbenchItem({
      resources: [boundAttachment, document, sameLookingUnbound],
      sessionKey: 'agent:main:webchat:test',
      title: 'Workbench',
    })
    expect(item.payload.resourceCount).toBe(2)
    expect(item.payload.resources).toBeUndefined()
  })

  it('keeps persisted inline Visualize widgets out of the Files projection', () => {
    const widget = resource('document', 'widget-doc')
    widget.source = 'show-widget'
    widget.visibility = 'internal'
    widget.presentation = 'inline'
    const workspaceFile = resource('document', 'workspace-doc')
    workspaceFile.source = 'workspace-preview'
    workspaceFile.visibility = 'internal'
    const otherInlineInternal = resource('document', 'other-inline-doc')
    otherInlineInternal.visibility = 'internal'
    otherInlineInternal.presentation = 'inline'

    expect(isInternalVisualizeResource(widget)).toBe(true)
    widget.source = 'show_widget:edit'
    expect(isInternalVisualizeResource(widget)).toBe(true)
    widget.source = 'show-widget'
    expect(isInternalVisualizeResource(workspaceFile)).toBe(false)
    expect(isInternalVisualizeResource(otherInlineInternal)).toBe(false)
    expect(canonicalWorkbenchResources([widget, workspaceFile, otherInlineInternal])).toEqual([
      workspaceFile,
      otherInlineInternal,
    ])
  })

  it('does not disguise a bound attachment as its mutable Document head', () => {
    const artifact = artifactPayloadFromWorkbenchResource(resource('attachment', 'att_1', {
      documentId: 'doc_1',
      headRevisionId: 'rev_2',
      headArtifactId: 'art_internal_2',
    }))
    expect(artifact.id).toBeUndefined()
    expect(artifact.documentId).toBeUndefined()
    expect(artifact.revisionId).toBeUndefined()
    expect(artifact.download_url).toBe('/resources/att_1')
    expect(artifact.sha256).toBe('attachment-sha')
    expect(artifact.workbenchResourceType).toBe('attachment')
  })

  it('keeps a published deliverable on its immutable artifact identity', () => {
    const artifact = artifactPayloadFromWorkbenchResource(resource('deliverable', 'art_1', {
      documentId: 'doc_1',
      headRevisionId: 'rev_2',
      headArtifactId: 'art_internal_2',
      publishedRevisionId: 'rev_1',
    }))
    expect(artifact).toMatchObject({
      id: 'art_1',
      sha256: 'deliverable-sha',
      download_url: '/resources/art_1',
      workbenchResourceType: 'deliverable',
    })
    expect(artifact.documentId).toBeUndefined()
    expect(artifact.revisionId).toBeUndefined()
  })

  it('uses native isolated HTML preview only for artifact-backed resources', () => {
    expect(resourceUsesNativeHtmlPreview(resource('deliverable', 'art_1'))).toBe(true)
    expect(resourceUsesNativeHtmlPreview(resource('document', 'doc_1', {
      documentId: 'doc_1',
      headRevisionId: 'rev_1',
      headArtifactId: 'art_internal_1',
    }))).toBe(true)
    expect(resourceUsesNativeHtmlPreview(resource('attachment', 'att_1'))).toBe(false)

    const textDeliverable = resource('deliverable', 'art_text')
    textDeliverable.name = 'notes.txt'
    textDeliverable.mime = 'text/plain'
    expect(resourceUsesNativeHtmlPreview(textDeliverable)).toBe(false)
  })

  it('binds a document preview to its exact head identities', () => {
    const artifact = artifactPayloadFromWorkbenchResource(resource('document', 'doc_1', {
      documentId: 'doc_1',
      headRevisionId: 'rev_2',
      headArtifactId: 'art_internal_2',
    }))
    expect(artifact).toMatchObject({
      id: 'art_internal_2',
      documentId: 'doc_1',
      revisionId: 'rev_2',
    })
  })

  it('uses the server-prepared launch target without mutating the resource', () => {
    const attachment = resource('attachment', 'att_1')
    const prepared = resourceFromPreparedPreview({
      resource: attachment,
      preview: {
        protocolVersion: 1,
        mode: 'isolated',
        resource: attachment.resource,
        launchUrl: '/prepared/att_1',
        sandboxProfile: 'opaque-offline',
        network: false,
        adapter: null,
      },
    })

    expect(prepared.downloadUrl).toBe('/prepared/att_1')
    expect(attachment.downloadUrl).toBe('/resources/att_1')
  })
})
