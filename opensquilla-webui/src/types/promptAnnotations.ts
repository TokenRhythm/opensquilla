export type PromptAnnotationStatus = 'draft' | 'sent' | 'discarded'
export type PromptAnnotationFreshness = 'fresh' | 'stale'
export type PromptAnnotationTargetStatus = 'ready' | 'contextual'
export type PromptAnnotationTargetReason = 'no_match' | 'ambiguous'

/** A user-selected page element. Locators are hints, never edit authority. */
export interface PromptAnnotationSelection {
  selectionId: string
  targetRef: string
  resourceId?: string
  tagName: string
  elementPath: string
  selectionText?: string
  locatorHint?: string
}

export interface PromptAnnotation {
  annotationId: string
  sessionKey: string
  sessionId?: string | null
  sessionEpoch?: number | null
  documentId: string
  documentName: string
  revisionId?: string
  generation?: number | null
  anchorId?: string
  body: string
  targetRef?: string
  resourceId?: string
  locatorHint?: string
  screenshotAttachment?: import('./chat').Attachment
  status: PromptAnnotationStatus
  freshness?: PromptAnnotationFreshness
  staleReason?: string | null
  stateRevision?: number
  tagName: string
  targetStatus?: PromptAnnotationTargetStatus
  targetReason?: PromptAnnotationTargetReason
  targetKind?: string
  targetText?: string
  locator?: Readonly<Record<string, unknown>>
  quote: string | null
  sourceExcerpt?: string | null
  sentMessageId?: string | null
  sentTurnId?: string | null
  sentOrder?: number | null
  createdAt: number | string | null
  updatedAt: number | string | null
  schemaVersion?: number
}

/** Immutable copy rendered under a sent user message. */
export interface PromptAnnotationSnapshot {
  annotationId: string
  documentId: string
  documentName: string
  revisionId?: string
  generation?: number | null
  anchorId?: string
  body: string
  targetRef?: string
  resourceId?: string
  locatorHint?: string
  tagName: string
  targetStatus?: PromptAnnotationTargetStatus
  targetReason?: PromptAnnotationTargetReason
  targetKind?: string
  targetText?: string
  locator?: Readonly<Record<string, unknown>>
  quote: string | null
  sourceExcerpt?: string | null
  sentOrder?: number
}

export interface PromptAnnotationCreateRequest {
  annotationId: string
  sessionKey: string
  documentId: string
  documentName?: string
  resourceId?: string
  selection: PromptAnnotationSelection
  body?: string
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
