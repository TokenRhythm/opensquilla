import type { RpcClientError } from '@/lib/rpc'

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

const CURRENT_CODES = new Set<string>(ARTIFACT_PRODUCT_ERROR_CODES)

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

const LEGACY_CODE_ALIASES: Readonly<Record<string, ArtifactProductErrorCode>> = {
  ARTIFACT_REVISION_CHANGED: 'DOCUMENT_CHANGED',
  ARTIFACT_SOURCE_CHANGED: 'DOCUMENT_CHANGED',
  ARTIFACT_DOCUMENT_CONFLICT: 'DOCUMENT_CHANGED',
  ARTIFACT_CHANGE_NOT_HEAD: 'DOCUMENT_CHANGED',
  ARTIFACT_CONFLICT: 'DOCUMENT_CHANGED',
  ARTIFACT_PREVIEW_CHANGED: 'DOCUMENT_CHANGED',
  ARTIFACT_SELECTION_CHANGED: 'DOCUMENT_CHANGED',
  DOCUMENT_RESOURCE_CONFLICT: 'DOCUMENT_CHANGED',
  DOCUMENT_MUTATION_CONFLICT: 'DOCUMENT_CHANGED',
  WORKBENCH_CURSOR_STALE: 'DOCUMENT_CHANGED',
  ARTIFACT_EDIT_SESSION_EXPIRED: 'EDIT_SESSION_RENEWAL_REQUIRED',
  ARTIFACT_EDIT_SESSION_STALE: 'EDIT_SESSION_RENEWAL_REQUIRED',
  ARTIFACT_EDIT_SESSION_CONFLICT: 'EDIT_SESSION_RENEWAL_REQUIRED',
  ARTIFACT_WRITER_LEASE_CONFLICT: 'WRITE_BUSY',
  STORAGE_BUSY: 'WRITE_BUSY',
  ARTIFACT_CHANGE_NOT_APPLIED: 'MUTATION_NOT_APPLIED',
  ARTIFACT_MUTATION_CLEANUP_AMBIGUOUS: 'MUTATION_OUTCOME_PENDING',
  ARTIFACT_ANNOTATION_NOT_DRAFT: 'ANNOTATION_UNAVAILABLE',
  ARTIFACT_FOCUS_UNAVAILABLE: 'ANNOTATION_UNAVAILABLE',
  ARTIFACT_FOCUS_UNSUPPORTED: 'ANNOTATION_UNAVAILABLE',
  ARTIFACT_SELECTION_UNAVAILABLE: 'ANNOTATION_UNAVAILABLE',
  ARTIFACT_SELECTION_UNSUPPORTED: 'ANNOTATION_UNAVAILABLE',
  ARTIFACT_SOURCE_ENCODING: 'RESOURCE_UNSUPPORTED',
  ARTIFACT_SOURCE_TOO_LARGE: 'RESOURCE_UNSUPPORTED',
  ARTIFACT_SOURCE_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  DOCUMENT_IMPORT_FORMAT_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  DOCUMENT_IMPORT_ENCODING_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  DOCUMENT_IMPORT_SIZE_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  DOCUMENT_IMPORT_HTML_INVALID: 'RESOURCE_UNSUPPORTED',
  DOCUMENT_BUNDLE_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  DOCUMENT_PUBLISH_FORMAT_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  WORKBENCH_PREVIEW_ENCODING_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  WORKBENCH_PREVIEW_UNSUPPORTED: 'RESOURCE_UNSUPPORTED',
  INVALID_PARAMS: 'INVALID_REQUEST',
  BAD_REQUEST: 'INVALID_REQUEST',
  NOT_FOUND: 'DOCUMENT_UNAVAILABLE',
  UNAVAILABLE: 'DOCUMENT_UNAVAILABLE',
  UNAUTHORIZED: 'PERMISSION_DENIED',
  RPC_TRANSPORT_ERROR: 'DOCUMENT_UNAVAILABLE',
  RPC_TIMEOUT: 'DOCUMENT_UNAVAILABLE',
}

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

function errorRecord(error: unknown): Partial<RpcClientError> & Record<string, unknown> {
  return error !== null && typeof error === 'object'
    ? error as Partial<RpcClientError> & Record<string, unknown>
    : {}
}

function canonicalCode(error: unknown): ArtifactProductErrorCode {
  const raw = errorRecord(error)
  const candidate = typeof raw.code === 'string' ? raw.code.trim().toUpperCase() : ''
  if (CURRENT_CODES.has(candidate)) return candidate as ArtifactProductErrorCode
  return LEGACY_CODE_ALIASES[candidate] || 'INTERNAL_ERROR'
}

/**
 * True only for errors whose code itself identifies the Artifact product
 * surface. Generic chat codes such as INVALID_REQUEST and INTERNAL_ERROR keep
 * their established chat presentation unless the caller has Artifact context.
 */
export function isKnownArtifactProductErrorCode(code: unknown): boolean {
  const candidate = typeof code === 'string' ? code.trim().toUpperCase() : ''
  if (ARTIFACT_SCOPED_CURRENT_CODES.has(candidate)) return true
  if (
    candidate.startsWith('ARTIFACT_')
    || candidate.startsWith('DOCUMENT_')
    || candidate.startsWith('WORKBENCH_')
  ) return candidate in LEGACY_CODE_ALIASES
  return false
}

/**
 * Convert every Artifact failure into a stable product category. Raw server
 * messages are deliberately ignored; diagnostics remain in Gateway logs.
 */
export function classifyArtifactProductError(
  error: unknown,
): ArtifactProductErrorClassification {
  const raw = errorRecord(error)
  const code = canonicalCode(error)
  const presentation = PRESENTATION[code]
  const rawRetryAfter = raw.retry_after_ms
  const retryAfter = Number(rawRetryAfter)
  return {
    code,
    messageKey: presentation.key,
    fallbackMessage: presentation.fallback,
    recovery: presentation.recovery,
    retryable: raw.retryable === true,
    retryAfterMs: rawRetryAfter !== null
      && rawRetryAfter !== undefined
      && Number.isFinite(retryAfter)
      && retryAfter >= 0
      ? retryAfter
      : null,
    accepted: typeof raw.accepted === 'boolean' ? raw.accepted : null,
  }
}

/** A transport loss can hide a committed write even without a server code. */
export function artifactMutationOutcomeMayBePending(error: unknown): boolean {
  const raw = errorRecord(error)
  if (raw.accepted === false) return false
  const candidate = typeof raw.code === 'string' ? raw.code.trim().toUpperCase() : ''
  return raw.accepted === null
    || candidate === 'RPC_TRANSPORT_ERROR'
    || candidate === 'RPC_TIMEOUT'
    || candidate === 'MUTATION_OUTCOME_PENDING'
    || candidate === 'ARTIFACT_MUTATION_CLEANUP_AMBIGUOUS'
}

export function artifactProductReasonCode(error: unknown): string | null {
  const details = errorRecord(error).details
  if (details === null || typeof details !== 'object') return null
  const reasonCode = (details as Record<string, unknown>).reasonCode
  return typeof reasonCode === 'string' && reasonCode.trim() ? reasonCode.trim() : null
}

export function artifactProductClientError(
  code: ArtifactProductErrorCode,
  options: { reasonCode?: string } = {},
): Error {
  const error = new Error(PRESENTATION[code].fallback) as RpcClientError
  error.code = code
  if (options.reasonCode) error.details = { reasonCode: options.reasonCode }
  return error
}
