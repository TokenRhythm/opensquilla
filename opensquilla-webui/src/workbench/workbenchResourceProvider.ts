import type {
  WorkbenchResource,
  WorkbenchResourceCapabilities,
  WorkbenchResourceRef,
  WorkbenchResourceRelations,
  WorkbenchResourceType,
} from '@/types/workbenchResources'

export type { WorkbenchResourceProvider } from '@/modules/artifactWorkbench'

export function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

export function valueAt(raw: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    if (raw[key] !== undefined) return raw[key]
  }
  return undefined
}

export function stringAt(raw: Record<string, unknown>, ...keys: string[]): string {
  const value = valueAt(raw, ...keys)
  return typeof value === 'string' ? value.trim() : ''
}

export function boolAt(raw: Record<string, unknown>, ...keys: string[]): boolean {
  return valueAt(raw, ...keys) === true
}

export function numberAt(raw: Record<string, unknown>, ...keys: string[]): number | undefined {
  const value = valueAt(raw, ...keys)
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

export function normalizeRef(value: unknown): WorkbenchResourceRef | null {
  const raw = record(value)
  if (!raw) return null
  const type = stringAt(raw, 'type', 'kind') as WorkbenchResourceType
  const legacyId = stringAt(raw, 'id', 'resourceId', 'resource_id')
  let canonicalId = ''
  switch (type) {
    case 'attachment':
      canonicalId = stringAt(raw, 'attachmentId', 'attachment_id')
      break
    case 'document':
      canonicalId = stringAt(raw, 'documentId', 'document_id')
      break
    case 'deliverable':
      canonicalId = stringAt(raw, 'artifactId', 'artifact_id')
      break
    case 'url':
      canonicalId = stringAt(raw, 'urlId', 'url_id')
      break
    default:
      return null
  }
  if (canonicalId && legacyId && canonicalId !== legacyId) return null
  const id = canonicalId || legacyId
  if (!id) return null
  switch (type) {
    case 'attachment':
      return { type, attachmentId: id, id }
    case 'document':
      return { type, documentId: id, id }
    case 'deliverable':
      return { type, artifactId: id, id }
    case 'url':
      return { type, urlId: id, id }
  }
}

export function serializeRef(value: WorkbenchResourceRef): Record<string, string> {
  const normalized = normalizeRef(value)
  if (!normalized) throw new Error('The workbench resource identity is invalid.')
  return normalized as Record<string, string>
}

export function normalizeCapabilities(value: unknown): WorkbenchResourceCapabilities {
  const raw = record(value) || {}
  const preview = boolAt(raw, 'preview')
  const legacyEdit = boolAt(raw, 'edit')
  const hasManualEdit = valueAt(raw, 'manualEdit', 'manual_edit') !== undefined
  const manualEdit = boolAt(raw, 'manualEdit', 'manual_edit')
    || (!hasManualEdit && legacyEdit)
  const agentEdit = boolAt(raw, 'agentEdit', 'agent_edit')
  const selectionContext = boolAt(
    raw,
    'selectionContext',
    'selection_context',
  )
  const edit = legacyEdit || manualEdit || agentEdit
  const legacyReason = stringAt(
    raw,
    'reasonCode',
    'reason_code',
    'unavailableReason',
    'unavailable_reason',
  ) || null
  const editReasonCode = stringAt(
    raw,
    'editReasonCode',
    'edit_reason_code',
  ) || (!edit ? legacyReason : null)
  const previewReasonCode = stringAt(
    raw,
    'previewReasonCode',
    'preview_reason_code',
  ) || (!preview ? legacyReason || editReasonCode : null)
  return {
    preview,
    download: boolAt(raw, 'download'),
    selectionContext,
    manualEdit,
    agentEdit,
    edit,
    publish: boolAt(raw, 'publish'),
    previewReasonCode,
    editReasonCode,
    reasonCode: legacyReason || editReasonCode || previewReasonCode,
  }
}

export function normalizeRelations(value: unknown): WorkbenchResourceRelations {
  const raw = record(value) || {}
  const source = normalizeRef(valueAt(raw, 'source'))
  return {
    documentId: stringAt(raw, 'documentId', 'document_id') || undefined,
    headRevisionId: stringAt(raw, 'headRevisionId', 'head_revision_id') || undefined,
    headArtifactId: stringAt(raw, 'headArtifactId', 'head_artifact_id') || undefined,
    source: source || undefined,
    deliverableId: stringAt(raw, 'deliverableId', 'deliverable_id') || undefined,
    publishedRevisionId: stringAt(
      raw,
      'publishedRevisionId',
      'published_revision_id',
    ) || undefined,
  }
}

export function normalizeWorkbenchResource(value: unknown): WorkbenchResource | null {
  const raw = record(value)
  if (!raw) return null
  const resource = normalizeRef(valueAt(raw, 'resource') || raw)
  const name = stringAt(raw, 'name')
  const mime = stringAt(raw, 'mime', 'mediaType', 'media_type')
  if (!resource || !name || !mime) return null
  return {
    resource,
    name,
    mime,
    size: numberAt(raw, 'size', 'byteSize', 'byte_size'),
    sha256: stringAt(raw, 'sha256') || undefined,
    createdAt: valueAt(raw, 'createdAt', 'created_at') as number | string | null | undefined,
    updatedAt: valueAt(raw, 'updatedAt', 'updated_at') as number | string | null | undefined,
    downloadUrl: stringAt(raw, 'downloadUrl', 'download_url') || undefined,
    capabilities: normalizeCapabilities(valueAt(raw, 'capabilities')),
    relations: normalizeRelations(valueAt(raw, 'relations')),
  }
}
