import {
  ArtifactProductFailure,
  type ArtifactProductErrorCode,
} from '@/utils/artifactProductErrors'
import { readTransportFailure } from './privateTransports'

const CURRENT_CODES = new Set<ArtifactProductErrorCode>([
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
  FORBIDDEN: 'PERMISSION_DENIED',
  RPC_TRANSPORT_ERROR: 'DOCUMENT_UNAVAILABLE',
  RPC_TIMEOUT: 'DOCUMENT_UNAVAILABLE',
}

function productCode(code: string | undefined): ArtifactProductErrorCode {
  const candidate = code?.trim().toUpperCase() || ''
  if (CURRENT_CODES.has(candidate as ArtifactProductErrorCode)) {
    return candidate as ArtifactProductErrorCode
  }
  return LEGACY_CODE_ALIASES[candidate] || 'INTERNAL_ERROR'
}

function artifactScopedWireCode(code: string | undefined): boolean {
  const candidate = code?.trim().toUpperCase() || ''
  return candidate.startsWith('ARTIFACT_')
    || candidate.startsWith('DOCUMENT_')
    || candidate.startsWith('WORKBENCH_')
    || CURRENT_CODES.has(candidate as ArtifactProductErrorCode)
      && candidate !== 'INVALID_REQUEST'
      && candidate !== 'INTERNAL_ERROR'
}

/** Project a wire/transport rejection into the Artifact product vocabulary. */
export function mapArtifactProductFailure(error: unknown): ArtifactProductFailure {
  if (error instanceof ArtifactProductFailure) return error
  const failure = readTransportFailure(error)
  const wireCode = failure.code?.trim().toUpperCase()
  const details = wireCode === 'ARTIFACT_ANNOTATION_NOT_DRAFT'
    ? {
        ...(failure.details && typeof failure.details === 'object' && !Array.isArray(failure.details)
          ? failure.details as Record<string, unknown>
          : {}),
        reasonCode: 'not_draft',
      }
    : failure.details
  const outcomeUncertain = failure.accepted === null
    || wireCode === 'RPC_TRANSPORT_ERROR'
    || wireCode === 'RPC_TIMEOUT'
    || wireCode === 'MUTATION_OUTCOME_PENDING'
    || wireCode === 'ARTIFACT_MUTATION_CLEANUP_AMBIGUOUS'
  return new ArtifactProductFailure(
    productCode(wireCode),
    failure.message,
    details,
    failure.retryable === true,
    failure.retryAfterMs ?? null,
    typeof failure.accepted === 'boolean' ? failure.accepted : null,
    outcomeUncertain,
    artifactScopedWireCode(wireCode),
  )
}
