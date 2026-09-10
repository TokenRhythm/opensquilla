import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'

/** Read-only compatibility for saved annotation cards; never grants editing authority. */
function objectValue(value: unknown): Record<string, unknown> | null {
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


function normalizedTargetStatus(raw: Record<string, unknown>): 'ready' | 'contextual' | undefined {
  const value = stringAt(raw, 'targetStatus', 'target_status').toLowerCase()
  return value === 'ready' || value === 'contextual' ? value : undefined
}

function normalizedTargetReason(raw: Record<string, unknown>): 'no_match' | 'ambiguous' | undefined {
  const value = stringAt(raw, 'targetReason', 'target_reason').toLowerCase()
  return value === 'no_match' || value === 'ambiguous' ? value : undefined
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
