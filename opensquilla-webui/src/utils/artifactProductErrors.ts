export const ARTIFACT_PRODUCT_ERROR_CODES = [
  'DOCUMENT_CHANGED',
  'EDIT_SESSION_RENEWAL_REQUIRED',
  'WRITE_BUSY',
  'MUTATION_NOT_APPLIED',
  'MUTATION_OUTCOME_PENDING',
  'DOCUMENT_UNAVAILABLE',
  'RESOURCE_UNSUPPORTED',
  'PERMISSION_DENIED',
  'PREVIEW_CAPABILITY_EXPIRED',
  'PREVIEW_RENDERER_FAILED',
  'ANNOTATION_UNAVAILABLE',
  'ANNOTATION_BUSY',
  'INVALID_REQUEST',
  'INTERNAL_ERROR',
] as const

export type ArtifactProductErrorCode = typeof ARTIFACT_PRODUCT_ERROR_CODES[number]

export type ArtifactProductRecoveryAction =
  | 'none'
  | 'retry-same-request'
  | 'retry-new-request'
  | 'reacquire-edit-session'
  | 'refresh-document'
  | 'reopen-preview'
  | 'ask-user'

export interface ArtifactProductErrorClassification {
  code: ArtifactProductErrorCode
  messageKey: `workbench.artifactErrors.${string}`
  fallbackMessage: string
  recovery: ArtifactProductRecoveryAction
  retryable: boolean
  retryAfterMs: number | null
  accepted: boolean | null
}

/** Product-owned failure shape; Gateway Adapters project transport failures here. */
export class ArtifactProductFailure extends Error {
  constructor(
    readonly code: ArtifactProductErrorCode,
    message: string,
    readonly details?: unknown,
    readonly retryable = false,
    readonly retryAfterMs: number | null = null,
    readonly accepted: boolean | null = null,
    readonly outcomeUncertain = false,
    readonly artifactScoped = true,
  ) {
    super(message)
    this.name = 'ArtifactProductFailure'
  }
}

const ARTIFACT_SCOPED_CURRENT_CODES = new Set<string>([
  'DOCUMENT_CHANGED',
  'EDIT_SESSION_RENEWAL_REQUIRED',
  'WRITE_BUSY',
  'MUTATION_NOT_APPLIED',
  'MUTATION_OUTCOME_PENDING',
  'DOCUMENT_UNAVAILABLE',
  'RESOURCE_UNSUPPORTED',
  'PREVIEW_CAPABILITY_EXPIRED',
  'PREVIEW_RENDERER_FAILED',
  'ANNOTATION_UNAVAILABLE',
  'ANNOTATION_BUSY',
])

const PRESENTATION: Readonly<Record<ArtifactProductErrorCode, {
  key: ArtifactProductErrorClassification['messageKey']
  fallback: string
  recovery: ArtifactProductRecoveryAction
}>> = {
  DOCUMENT_CHANGED: {
    key: 'workbench.artifactErrors.documentChanged',
    fallback: 'The page changed. Refresh it before trying again.',
    recovery: 'refresh-document',
  },
  EDIT_SESSION_RENEWAL_REQUIRED: {
    key: 'workbench.artifactErrors.editSessionRenewalRequired',
    fallback: 'Editing is reconnecting. Your unsaved changes are still available.',
    recovery: 'reacquire-edit-session',
  },
  WRITE_BUSY: {
    key: 'workbench.artifactErrors.writeBusy',
    fallback: 'The page is being updated. Wait a moment and try again.',
    recovery: 'retry-same-request',
  },
  MUTATION_NOT_APPLIED: {
    key: 'workbench.artifactErrors.mutationNotApplied',
    fallback: 'The page was not updated. You can try again.',
    recovery: 'retry-new-request',
  },
  MUTATION_OUTCOME_PENDING: {
    key: 'workbench.artifactErrors.mutationOutcomePending',
    fallback: 'The update result cannot be confirmed. Open the page to check.',
    recovery: 'ask-user',
  },
  DOCUMENT_UNAVAILABLE: {
    key: 'workbench.artifactErrors.documentUnavailable',
    fallback: 'This page is temporarily unavailable. Try again.',
    recovery: 'retry-same-request',
  },
  RESOURCE_UNSUPPORTED: {
    key: 'workbench.artifactErrors.resourceUnsupported',
    fallback: 'This file cannot be edited here.',
    recovery: 'none',
  },
  PERMISSION_DENIED: {
    key: 'workbench.artifactErrors.permissionDenied',
    fallback: 'You do not have permission to update this page.',
    recovery: 'none',
  },
  PREVIEW_CAPABILITY_EXPIRED: {
    key: 'workbench.artifactErrors.previewCapabilityExpired',
    fallback: 'The preview needs to be reopened.',
    recovery: 'reopen-preview',
  },
  PREVIEW_RENDERER_FAILED: {
    key: 'workbench.artifactErrors.previewRendererFailed',
    fallback: 'The preview could not be displayed. Try reopening it.',
    recovery: 'reopen-preview',
  },
  ANNOTATION_UNAVAILABLE: {
    key: 'workbench.artifactErrors.annotationUnavailable',
    fallback: 'This annotation is temporarily unavailable.',
    recovery: 'retry-same-request',
  },
  ANNOTATION_BUSY: {
    key: 'workbench.artifactErrors.annotationBusy',
    fallback: 'Annotations are being updated. Wait a moment and try again.',
    recovery: 'retry-same-request',
  },
  INVALID_REQUEST: {
    key: 'workbench.artifactErrors.invalidRequest',
    fallback: 'The request could not be completed. Check the input and try again.',
    recovery: 'none',
  },
  INTERNAL_ERROR: {
    key: 'workbench.artifactErrors.internalError',
    fallback: 'The operation could not be completed. Try again.',
    recovery: 'retry-same-request',
  },
}

/**
 * True only for errors whose code itself identifies the Artifact product
 * surface. Generic chat codes such as INVALID_REQUEST and INTERNAL_ERROR keep
 * their established chat presentation unless the caller has Artifact context.
 */
export function isKnownArtifactProductErrorCode(code: unknown): boolean {
  const candidate = typeof code === 'string' ? code.trim().toUpperCase() : ''
  return ARTIFACT_SCOPED_CURRENT_CODES.has(candidate)
}

/**
 * Convert every Artifact failure into a stable product category. Raw server
 * messages are deliberately ignored; diagnostics remain in Gateway logs.
 */
export function classifyArtifactProductError(
  error: unknown,
): ArtifactProductErrorClassification {
  const failure = error instanceof ArtifactProductFailure ? error : null
  const code = failure?.code ?? 'INTERNAL_ERROR'
  const presentation = PRESENTATION[code]
  return {
    code,
    messageKey: presentation.key,
    fallbackMessage: presentation.fallback,
    recovery: presentation.recovery,
    retryable: failure?.retryable === true,
    retryAfterMs: failure?.retryAfterMs ?? null,
    accepted: failure?.accepted ?? null,
  }
}

/** A transport loss can hide a committed write even without a server code. */
export function artifactMutationOutcomeMayBePending(error: unknown): boolean {
  if (!(error instanceof ArtifactProductFailure) || error.accepted === false) return false
  return error.outcomeUncertain || error.code === 'MUTATION_OUTCOME_PENDING'
}

export function artifactProductReasonCode(error: unknown): string | null {
  const details = error instanceof ArtifactProductFailure ? error.details : null
  if (details === null || typeof details !== 'object') return null
  const reasonCode = (details as Record<string, unknown>).reasonCode
  return typeof reasonCode === 'string' && reasonCode.trim() ? reasonCode.trim() : null
}

export function artifactProductClientError(
  code: ArtifactProductErrorCode,
  options: { reasonCode?: string } = {},
): Error {
  return new ArtifactProductFailure(
    code,
    PRESENTATION[code].fallback,
    options.reasonCode ? { reasonCode: options.reasonCode } : undefined,
  )
}
