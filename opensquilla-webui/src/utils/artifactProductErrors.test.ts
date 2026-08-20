import { describe, expect, it } from 'vitest'

import {
  artifactMutationOutcomeMayBePending,
  artifactProductClientError,
  artifactProductReasonCode,
  classifyArtifactProductError,
  isKnownArtifactProductErrorCode,
} from './artifactProductErrors'

describe('artifact product error classification', () => {
  it('maps compatibility codes without exposing their raw messages', () => {
    const error = Object.assign(new Error('lease 44 on revision abc failed'), {
      code: 'ARTIFACT_EDIT_SESSION_STALE',
      retryable: true,
    })

    expect(classifyArtifactProductError(error)).toMatchObject({
      code: 'EDIT_SESSION_RENEWAL_REQUIRED',
      messageKey: 'workbench.artifactErrors.editSessionRenewalRequired',
      recovery: 'reacquire-edit-session',
      retryable: true,
    })
    expect(classifyArtifactProductError(error).fallbackMessage)
      .not.toContain('lease 44')
  })

  it.each([
    ['ARTIFACT_ANNOTATION_NOT_DRAFT', 'ANNOTATION_UNAVAILABLE'],
    ['ARTIFACT_CHANGE_NOT_APPLIED', 'MUTATION_NOT_APPLIED'],
    ['ARTIFACT_CONFLICT', 'DOCUMENT_CHANGED'],
    ['ARTIFACT_FOCUS_UNAVAILABLE', 'ANNOTATION_UNAVAILABLE'],
    ['ARTIFACT_FOCUS_UNSUPPORTED', 'ANNOTATION_UNAVAILABLE'],
    ['ARTIFACT_PREVIEW_CHANGED', 'DOCUMENT_CHANGED'],
    ['ARTIFACT_SELECTION_CHANGED', 'DOCUMENT_CHANGED'],
    ['ARTIFACT_SELECTION_UNAVAILABLE', 'ANNOTATION_UNAVAILABLE'],
    ['ARTIFACT_SELECTION_UNSUPPORTED', 'ANNOTATION_UNAVAILABLE'],
    ['ARTIFACT_SOURCE_ENCODING', 'RESOURCE_UNSUPPORTED'],
    ['ARTIFACT_SOURCE_TOO_LARGE', 'RESOURCE_UNSUPPORTED'],
    ['ARTIFACT_SOURCE_UNSUPPORTED', 'RESOURCE_UNSUPPORTED'],
    ['DOCUMENT_PUBLISH_FORMAT_UNSUPPORTED', 'RESOURCE_UNSUPPORTED'],
    ['WORKBENCH_CURSOR_STALE', 'DOCUMENT_CHANGED'],
    ['WORKBENCH_PREVIEW_ENCODING_UNSUPPORTED', 'RESOURCE_UNSUPPORTED'],
    ['WORKBENCH_PREVIEW_UNSUPPORTED', 'RESOURCE_UNSUPPORTED'],
    ['INVALID_PARAMS', 'INVALID_REQUEST'],
    ['BAD_REQUEST', 'INVALID_REQUEST'],
  ] as const)('maps legacy %s to the stable %s recovery category', (legacy, stable) => {
    expect(classifyArtifactProductError(Object.assign(new Error('private detail'), {
      code: legacy,
    })).code).toBe(stable)
  })

  it('uses a safe internal category for unknown raw failures', () => {
    const classified = classifyArtifactProductError(new Error('sqlite row private-value'))
    expect(classified.code).toBe('INTERNAL_ERROR')
    expect(classified.fallbackMessage).not.toContain('private-value')
    expect(artifactMutationOutcomeMayBePending(new Error('local validation failed'))).toBe(false)
  })

  it('does not query a known rejected write and preserves pending transport writes', () => {
    expect(artifactMutationOutcomeMayBePending(Object.assign(new Error('rejected'), {
      code: 'DOCUMENT_CHANGED',
      accepted: false,
    }))).toBe(false)
    expect(artifactMutationOutcomeMayBePending(Object.assign(new Error('timeout'), {
      code: 'RPC_TIMEOUT',
    }))).toBe(true)
    expect(artifactMutationOutcomeMayBePending(Object.assign(new Error('closed'), {
      code: 'RPC_TRANSPORT_ERROR',
      accepted: null,
    }))).toBe(true)
    expect(artifactMutationOutcomeMayBePending(Object.assign(new Error('not connected'), {
      code: 'RPC_TRANSPORT_ERROR',
      accepted: false,
    }))).toBe(false)
  })

  it('presents read transport failures as unavailable while writes resolve uncertainty', () => {
    const timeout = Object.assign(new Error('private timeout detail'), {
      code: 'RPC_TIMEOUT',
    })
    expect(classifyArtifactProductError(timeout)).toMatchObject({
      code: 'DOCUMENT_UNAVAILABLE',
      recovery: 'retry-same-request',
    })
    expect(artifactMutationOutcomeMayBePending(timeout)).toBe(true)
  })

  it('creates safe client-side errors with the stable code attached', () => {
    const error = artifactProductClientError('DOCUMENT_UNAVAILABLE') as Error & { code?: string }
    expect(error.code).toBe('DOCUMENT_UNAVAILABLE')
    expect(error.message).toBe('This page is temporarily unavailable. Try again.')
  })

  it('carries only a stable reason code for localized unsupported resources', () => {
    const error = artifactProductClientError('RESOURCE_UNSUPPORTED', {
      reasonCode: 'html_encoding_unsupported',
    })
    expect(artifactProductReasonCode(error)).toBe('html_encoding_unsupported')
    expect(artifactProductReasonCode(new Error('localized text is not protocol'))).toBeNull()
  })

  it('identifies artifact-scoped codes without claiming generic chat failures', () => {
    expect(isKnownArtifactProductErrorCode('ANNOTATION_UNAVAILABLE')).toBe(true)
    expect(isKnownArtifactProductErrorCode('DOCUMENT_IMPORT_FORMAT_UNSUPPORTED')).toBe(true)
    expect(isKnownArtifactProductErrorCode('WORKBENCH_PREVIEW_UNSUPPORTED')).toBe(true)
    expect(isKnownArtifactProductErrorCode('INVALID_REQUEST')).toBe(false)
    expect(isKnownArtifactProductErrorCode('INTERNAL_ERROR')).toBe(false)
    expect(isKnownArtifactProductErrorCode('SOME_CHAT_FAILURE')).toBe(false)
  })
})
