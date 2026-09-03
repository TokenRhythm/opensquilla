import type {
  PromptAnnotation,
  PromptAnnotationFreshness,
  PromptAnnotationSnapshot,
  PromptAnnotationStatus,
} from '@/types/promptAnnotations'

export type { ArtifactPromptAnnotationProvider } from '@/modules/artifactWorkbench'

export function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function valueAt(raw: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    if (raw[key] !== undefined) return raw[key]
  }
  return undefined
}

function stringAt(raw: Record<string, unknown>, ...keys: string[]): string {
  const value = valueAt(raw, ...keys)
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function nullableStringAt(raw: Record<string, unknown>, ...keys: string[]): string | null {
  return stringAt(raw, ...keys).trim() || null
}

function numberAt(raw: Record<string, unknown>, fallback: number, ...keys: string[]): number {
  const value = Number(valueAt(raw, ...keys))
  return Number.isFinite(value) ? value : fallback
}

function timestampAt(raw: Record<string, unknown>, ...keys: string[]): number | string | null {
  const value = valueAt(raw, ...keys)
  return typeof value === 'number' || typeof value === 'string' ? value : null
}

function normalizedStatus(raw: Record<string, unknown>): PromptAnnotationStatus {
  const status = stringAt(raw, 'status').toLowerCase()
  return status === 'sent' || status === 'discarded' ? status : 'draft'
}

function normalizedFreshness(raw: Record<string, unknown>): PromptAnnotationFreshness {
  const value = stringAt(raw, 'freshness').toLowerCase()
  if (value === 'stale') return 'stale'
  if (value === 'fresh' || value === 'current') return 'fresh'
  return valueAt(raw, 'fresh', 'isFresh') === false || nullableStringAt(raw, 'staleReason', 'stale_reason')
    ? 'stale'
    : 'fresh'
}

function normalizedTargetStatus(raw: Record<string, unknown>): 'ready' | 'contextual' | undefined {
  const value = stringAt(raw, 'targetStatus', 'target_status').toLowerCase()
  return value === 'ready' || value === 'contextual' ? value : undefined
}

function normalizedTargetReason(raw: Record<string, unknown>): 'no_match' | 'ambiguous' | undefined {
  const value = stringAt(raw, 'targetReason', 'target_reason').toLowerCase()
  return value === 'no_match' || value === 'ambiguous' ? value : undefined
}

export function normalizePromptAnnotation(
  value: unknown,
  defaults: { sessionKey?: string } = {},
): PromptAnnotation | null {
  const raw = objectValue(value)
  if (!raw) return null
  const annotationId = stringAt(raw, 'annotationId', 'annotation_id', 'id').trim()
  const sessionKey = (stringAt(raw, 'sessionKey', 'session_key') || defaults.sessionKey || '').trim()
  const documentId = stringAt(raw, 'documentId', 'document_id').trim()
  const revisionId = stringAt(raw, 'revisionId', 'revision_id').trim()
  const anchorId = stringAt(raw, 'anchorId', 'anchor_id').trim()
  if (!annotationId || !sessionKey || !documentId || !revisionId || !anchorId) return null
  const anchor = objectValue(valueAt(raw, 'anchor'))
  const locator = objectValue(valueAt(raw, 'locator'))
    || (anchor ? objectValue(valueAt(anchor, 'locator')) : null)
    || {}
  const tagName = stringAt(raw, 'tagName', 'tag_name')
    || String(locator.tagName || locator.tag_name || '')
  return {
    annotationId,
    sessionKey,
    sessionId: nullableStringAt(raw, 'sessionId', 'session_id'),
    sessionEpoch: valueAt(raw, 'sessionEpoch', 'session_epoch') == null
      ? null
      : Math.max(0, numberAt(raw, 0, 'sessionEpoch', 'session_epoch')),
    documentId,
    documentName: stringAt(raw, 'documentName', 'document_name', 'name') || 'artifact',
    revisionId,
    generation: valueAt(raw, 'generation') == null
      ? null
      : Math.max(1, numberAt(raw, 1, 'generation')),
    anchorId,
    body: stringAt(raw, 'body'),
    status: normalizedStatus(raw),
    freshness: normalizedFreshness(raw),
    staleReason: nullableStringAt(raw, 'staleReason', 'stale_reason'),
    stateRevision: Math.max(1, numberAt(raw, 1, 'stateRevision', 'state_revision')),
    tagName: tagName.toLowerCase(),
    ...(normalizedTargetStatus(raw) ? { targetStatus: normalizedTargetStatus(raw) } : {}),
    ...(normalizedTargetReason(raw) ? { targetReason: normalizedTargetReason(raw) } : {}),
    ...(stringAt(raw, 'targetKind', 'target_kind').trim()
      ? { targetKind: stringAt(raw, 'targetKind', 'target_kind').trim().toLowerCase() }
      : {}),
    ...(stringAt(raw, 'targetText', 'target_text').trim()
      ? { targetText: stringAt(raw, 'targetText', 'target_text').trim().slice(0, 160) }
      : {}),
    locator,
    quote: nullableStringAt(raw, 'quote')
      || (anchor ? nullableStringAt(anchor, 'quote') : null),
    sourceExcerpt: nullableStringAt(raw, 'sourceExcerpt', 'source_excerpt'),
    sentMessageId: nullableStringAt(raw, 'sentMessageId', 'sent_message_id'),
    sentTurnId: nullableStringAt(raw, 'sentTurnId', 'sent_turn_id'),
    sentOrder: valueAt(raw, 'sentOrder', 'sent_order') == null
      ? null
      : Math.max(0, numberAt(raw, 0, 'sentOrder', 'sent_order')),
    createdAt: timestampAt(raw, 'createdAt', 'created_at'),
    updatedAt: timestampAt(raw, 'updatedAt', 'updated_at'),
    schemaVersion: Math.max(1, numberAt(raw, 1, 'schemaVersion', 'schema_version')),
  }
}

export function normalizePromptAnnotationSnapshot(
  value: unknown,
  fallbackOrder = 0,
): PromptAnnotationSnapshot | null {
  const raw = objectValue(value)
  if (!raw) return null
  const document = objectValue(valueAt(raw, 'document'))
  const revision = objectValue(valueAt(raw, 'revision'))
  const anchor = objectValue(valueAt(raw, 'anchor'))
  const annotationId = stringAt(raw, 'annotationId', 'annotation_id', 'id').trim()
  const documentId = (stringAt(raw, 'documentId', 'document_id')
    || (document ? stringAt(document, 'id', 'documentId', 'document_id') : '')).trim()
  const revisionId = (stringAt(raw, 'revisionId', 'revision_id')
    || (revision ? stringAt(revision, 'id', 'revisionId', 'revision_id') : '')).trim()
  const anchorId = (stringAt(raw, 'anchorId', 'anchor_id')
    || (anchor ? stringAt(anchor, 'id', 'anchorId', 'anchor_id') : '')).trim()
  if (!annotationId || !documentId || !revisionId || !anchorId) return null
  const locator = objectValue(valueAt(raw, 'locator'))
    || (anchor ? objectValue(valueAt(anchor, 'locator')) : null)
    || {}
  return {
    annotationId,
    documentId,
    documentName: stringAt(raw, 'documentName', 'document_name', 'name')
      || (document ? stringAt(document, 'name') : '')
      || 'artifact',
    revisionId,
    generation: valueAt(raw, 'generation') == null
      && (!revision || valueAt(revision, 'generation') == null)
      ? null
      : Math.max(1, revision && valueAt(raw, 'generation') == null
        ? numberAt(revision, 1, 'generation')
        : numberAt(raw, 1, 'generation')),
    anchorId,
    body: stringAt(raw, 'body'),
    tagName: (stringAt(raw, 'tagName', 'tag_name')
      || (anchor ? stringAt(anchor, 'tagName', 'tag_name') : '')
      || String(locator.tagName || locator.tag_name || '')).toLowerCase(),
    ...(normalizedTargetStatus(raw) ? { targetStatus: normalizedTargetStatus(raw) } : {}),
    ...(normalizedTargetReason(raw) ? { targetReason: normalizedTargetReason(raw) } : {}),
    ...(stringAt(raw, 'targetKind', 'target_kind').trim()
      ? { targetKind: stringAt(raw, 'targetKind', 'target_kind').trim().toLowerCase() }
      : {}),
    ...(stringAt(raw, 'targetText', 'target_text').trim()
      ? { targetText: stringAt(raw, 'targetText', 'target_text').trim().slice(0, 160) }
      : {}),
    locator,
    quote: nullableStringAt(raw, 'quote')
      || (anchor ? nullableStringAt(anchor, 'quote') : null),
    sourceExcerpt: nullableStringAt(raw, 'sourceExcerpt', 'source_excerpt'),
    sentOrder: Math.max(0, numberAt(
      raw,
      fallbackOrder,
      'sentOrder',
      'sent_order',
      'order',
    )),
  }
}
