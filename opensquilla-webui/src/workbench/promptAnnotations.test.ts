// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'

import {
  ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT,
  ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT,
  focusArtifactPromptAnnotation,
  reuseArtifactPromptAnnotation,
  type ArtifactPromptAnnotationFocusDetail,
  type ArtifactPromptAnnotationReuseDetail,
} from './promptAnnotations'

afterEach(() => {
  document.body.innerHTML = ''
})

describe('prompt annotation Workbench activation', () => {
  it('waits until the trusted Workbench reports the native surface ready', async () => {
    const listener = (event: Event) => {
      const detail = (event as CustomEvent<ArtifactPromptAnnotationFocusDetail>).detail
      expect(detail).toMatchObject({
        annotationId: 'annotation-1',
        documentId: 'document-1',
        sessionKey: 'session-a',
      })
      detail.acknowledge?.()
      window.setTimeout(() => detail.complete?.(true), 0)
    }
    window.addEventListener(ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT, listener, { once: true })

    await expect(focusArtifactPromptAnnotation({
      annotationId: 'annotation-1',
      documentId: 'document-1',
      sessionKey: 'session-a',
    })).resolves.toBe(true)
  })

  it('fails closed when no Workbench accepts the request', async () => {
    await expect(focusArtifactPromptAnnotation({
      annotationId: 'annotation-1',
      documentId: 'document-1',
      sessionKey: 'session-a',
    })).resolves.toBe(false)
  })

  it('reuses only body and document scope, never a historical locator', async () => {
    const listener = (event: Event) => {
      const detail = (event as CustomEvent<ArtifactPromptAnnotationReuseDetail>).detail
      expect(detail).toMatchObject({
        body: 'Make this concise.',
        documentId: 'document-1',
        sessionKey: 'session-a',
      })
      expect(detail).not.toHaveProperty('locator')
      expect(detail).not.toHaveProperty('anchorId')
      detail.acknowledge?.()
      detail.complete?.(true)
    }
    window.addEventListener(ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT, listener, { once: true })

    await expect(reuseArtifactPromptAnnotation({
      body: 'Make this concise.',
      documentId: 'document-1',
      sessionKey: 'session-a',
    })).resolves.toBe(true)
  })
})
