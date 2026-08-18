export type PromptAnnotationStatus = 'draft' | 'sent' | 'discarded'
export type PromptAnnotationFreshness = 'fresh' | 'stale'
export type PromptAnnotationTargetStatus = 'ready' | 'contextual'
export type PromptAnnotationTargetReason = 'no_match' | 'ambiguous'

/**
 * A trusted, server-validated description of the element selected in the
 * native HTML preview. Renderer supplied candidates are never treated as
 * anchors until the Gateway accepts them.
 */
export interface PromptAnnotationSelection {
  selectionId: string
  tagName: string
  elementPath: string
  /** Proof for the selected element and its source-mappable ancestor path. */
  elementProofSha256: string
  /** Compatibility diagnostic only; never authorizes an annotation anchor. */
  domSha256?: string
}

export interface PromptAnnotation {
  annotationId: string
  sessionKey: string
  sessionId: string | null
  sessionEpoch: number | null
  documentId: string
  documentName: string
  revisionId: string
  generation: number | null
  anchorId: string
  body: string
  status: PromptAnnotationStatus
  freshness: PromptAnnotationFreshness
  staleReason: string | null
  stateRevision: number
  tagName: string
  targetStatus?: PromptAnnotationTargetStatus
  targetReason?: PromptAnnotationTargetReason
  targetKind?: string
  targetText?: string
  locator: Readonly<Record<string, unknown>>
  quote: string | null
  sourceExcerpt: string | null
  sentMessageId: string | null
  sentTurnId: string | null
  sentOrder: number | null
  createdAt: number | string | null
  updatedAt: number | string | null
  schemaVersion: number
}

/** Immutable copy rendered under a sent user message. */
export interface PromptAnnotationSnapshot {
  annotationId: string
  documentId: string
  documentName: string
  revisionId: string
  generation: number | null
  anchorId: string
  body: string
  tagName: string
  targetStatus?: PromptAnnotationTargetStatus
  targetReason?: PromptAnnotationTargetReason
  targetKind?: string
  targetText?: string
  locator: Readonly<Record<string, unknown>>
  quote: string | null
  sourceExcerpt: string | null
  sentOrder: number
}

export interface PromptAnnotationCreateRequest {
  annotationId: string
  sessionKey: string
  documentId: string
  revisionId: string
  selection: PromptAnnotationSelection
  body?: string
}

export interface PromptAnnotationUpdateRequest {
  annotationId: string
  sessionKey: string
  body: string
  expectedStateRevision: number
}

export interface PromptAnnotationDiscardRequest {
  annotationId: string
  sessionKey: string
  expectedStateRevision: number
}

/**
 * A renderer-safe request to focus a persisted draft. The Gateway resolves the
 * trusted anchor and current Desktop surface; locators never cross this API.
 */
export interface PromptAnnotationFocusRequest {
  annotationId: string
  sessionKey: string
}

export interface PromptAnnotationFocusResult {
  focused: true
  annotationId: string
  documentId: string
}

export interface PromptAnnotationsListResponse {
  annotations?: unknown[]
}

export interface PromptAnnotationResponse {
  annotation?: unknown
}

export interface PromptAnnotationFocusResponse {
  focused?: unknown
  annotationId?: unknown
  documentId?: unknown
}

export const PROMPT_ANNOTATION_MAX_COUNT = 16
export const PROMPT_ANNOTATION_MAX_BODY_BYTES = 16 * 1024
// A code-unit maxlength remains a useful input upper bound. Authority and
// send checks use the UTF-8 helper because the server's limit is byte-based.
export const PROMPT_ANNOTATION_MAX_BODY_LENGTH = PROMPT_ANNOTATION_MAX_BODY_BYTES

const promptAnnotationTextEncoder = new TextEncoder()

export function promptAnnotationBodyByteLength(body: string): number {
  return promptAnnotationTextEncoder.encode(body).byteLength
}

export function promptAnnotationBodyWithinLimit(body: string): boolean {
  return promptAnnotationBodyByteLength(body) <= PROMPT_ANNOTATION_MAX_BODY_BYTES
}
