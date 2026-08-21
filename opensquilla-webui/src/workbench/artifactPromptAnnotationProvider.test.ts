import { describe, expect, it, vi } from 'vitest'

import {
  createRpcArtifactPromptAnnotationProvider,
  normalizePromptAnnotationSnapshot,
  PROMPT_ANNOTATION_RPC_METHODS,
} from './artifactPromptAnnotationProvider'
import type { PromptAnnotationSelection } from '@/types/promptAnnotations'

function serverAnnotation(overrides: Record<string, unknown> = {}) {
  return {
    id: 'annotation-1',
    documentId: 'document-1',
    revisionId: 'revision-1',
    anchorId: 'anchor-1',
    anchor: {
      locator: {
        start_offset: 0,
        start_tag_end_offset: 12,
        tag_name: 'section',
        source_sha256: 'a'.repeat(64),
        offset_encoding: 'unicode-code-point',
      },
      quote: '<section>',
    },
    body: 'Make this clearer.',
    status: 'draft',
    freshness: 'current',
    stateRevision: 1,
    schemaVersion: 1,
    ...overrides,
  }
}

describe('artifact prompt annotation RPC provider', () => {
  it('normalizes the nested immutable snapshot returned by chat history', () => {
    expect(normalizePromptAnnotationSnapshot({
      version: 1,
      annotationId: 'annotation-history-1',
      order: 2,
      body: 'Make this heading shorter.',
      document: { id: 'document-1', name: 'page.html', kind: 'html' },
      revision: { id: 'revision-3', generation: 3, sha256: 'a'.repeat(64) },
      anchor: {
        id: 'anchor-2',
        kind: 'dom_source',
        tagName: 'H1',
        locator: { start_offset: 7 },
        quote: '<h1>',
      },
      targetStatus: 'contextual',
      targetReason: 'no_match',
      targetKind: 'heading',
      targetText: 'Quarterly results',
    })).toEqual(expect.objectContaining({
      annotationId: 'annotation-history-1',
      documentId: 'document-1',
      documentName: 'page.html',
      revisionId: 'revision-3',
      generation: 3,
      anchorId: 'anchor-2',
      tagName: 'h1',
      quote: '<h1>',
      targetStatus: 'contextual',
      targetReason: 'no_match',
      targetKind: 'heading',
      targetText: 'Quarterly results',
      sentOrder: 2,
    }))
  })

  it('restores only draft annotations and hydrates the scoped session key', async () => {
    const call = vi.fn().mockResolvedValue({
      annotations: [serverAnnotation({
        targetStatus: 'ready',
        targetKind: 'section',
        targetText: 'Overview',
      })],
    })
    const provider = createRpcArtifactPromptAnnotationProvider({ call })

    const annotations = await provider.list('session-a')

    expect(call).toHaveBeenCalledWith(
      PROMPT_ANNOTATION_RPC_METHODS.list,
      { sessionKey: 'session-a', status: 'draft' },
      expect.objectContaining({ timeoutMs: 10_000 }),
    )
    expect(annotations).toEqual([
      expect.objectContaining({
        annotationId: 'annotation-1',
        sessionKey: 'session-a',
        freshness: 'fresh',
        tagName: 'section',
        targetStatus: 'ready',
        targetKind: 'section',
        targetText: 'Overview',
        quote: '<section>',
      }),
    ])
  })

  it('sends only the bounded native selection contract when creating a draft', async () => {
    const call = vi.fn().mockResolvedValue({ annotation: serverAnnotation() })
    const provider = createRpcArtifactPromptAnnotationProvider({ call })

    await provider.create({
      annotationId: 'annotation-1',
      sessionKey: 'session-a',
      documentId: 'document-1',
      revisionId: 'revision-1',
      selection: {
        selectionId: 'selection-1',
        tagName: 'SECTION',
        elementPath: '[["","section",1]]',
        elementProofSha256: 'c'.repeat(64),
        domSha256: 'b'.repeat(64),
        sourceSha256: 'must-not-cross-the-renderer-boundary',
        startOffset: 99,
      } as PromptAnnotationSelection & { sourceSha256: string; startOffset: number },
    })

    expect(call).toHaveBeenCalledWith(
      PROMPT_ANNOTATION_RPC_METHODS.create,
      {
        annotationId: 'annotation-1',
        sessionKey: 'session-a',
        documentId: 'document-1',
        revisionId: 'revision-1',
        selection: {
          selectionId: 'selection-1',
          tagName: 'SECTION',
          elementPath: '[["","section",1]]',
          elementProofSha256: 'c'.repeat(64),
          domSha256: 'b'.repeat(64),
        },
      },
      expect.any(Object),
    )
  })

  it('focuses only by scoped opaque IDs and validates the trusted response', async () => {
    const call = vi.fn().mockResolvedValue({
      focused: true,
      annotationId: 'annotation-1',
      documentId: 'document-1',
      locator: { mustNotBeTrusted: true },
    })
    const provider = createRpcArtifactPromptAnnotationProvider({ call })

    await expect(provider.focus({
      sessionKey: 'session-a',
      annotationId: 'annotation-1',
    })).resolves.toEqual({
      focused: true,
      annotationId: 'annotation-1',
      documentId: 'document-1',
    })
    expect(call).toHaveBeenCalledWith(
      PROMPT_ANNOTATION_RPC_METHODS.focus,
      { sessionKey: 'session-a', annotationId: 'annotation-1' },
      expect.objectContaining({ timeoutMs: 10_000 }),
    )

    call.mockResolvedValueOnce({
      focused: true,
      annotationId: 'another-annotation',
      documentId: 'document-1',
    })
    await expect(provider.focus({
      sessionKey: 'session-a',
      annotationId: 'annotation-1',
    })).resolves.toBeNull()
  })

  it('marks unsupported optional RPCs unavailable without retaining fake drafts', async () => {
    const markMethodUnavailable = vi.fn()
    const provider = createRpcArtifactPromptAnnotationProvider({
      call: vi.fn().mockRejectedValue(Object.assign(new Error('Method not found'), {
        code: 'METHOD_NOT_FOUND',
      })),
      markMethodUnavailable,
    })

    await expect(provider.list('session-a')).resolves.toEqual([])
    expect(markMethodUnavailable).toHaveBeenCalledWith(PROMPT_ANNOTATION_RPC_METHODS.list)
  })
})
