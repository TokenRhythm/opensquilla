import type {
  ArtifactActorKind,
  ArtifactAnchor,
  ArtifactAnchorKind,
  ArtifactAnchorState,
  ArtifactChangeSet,
  ArtifactChangeSetStatus,
  ArtifactDocument,
  ArtifactDocumentCapabilities,
  ArtifactDocumentKind,
  ArtifactDocumentWorkspace,
  ArtifactEditCapabilities,
  ArtifactEditSession,
  ArtifactRevision,
  ArtifactRevisionSource,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/artifacts'
import { isOfficeArtifact } from '@/utils/chat/artifacts'

export type { ArtifactDocumentProvider } from '@/modules/artifactWorkbench'
export { isOfficeArtifact } from '@/utils/chat/artifacts'

const DOCUMENT_KINDS = new Set<ArtifactDocumentKind>([
  'document', 'spreadsheet', 'presentation', 'html', 'other',
])
const ACTOR_KINDS = new Set<ArtifactActorKind>(['user', 'agent', 'system'])
const REVISION_SOURCES = new Set<ArtifactRevisionSource>([
  'initial', 'manual', 'agent', 'restore', 'revert',
])
const CHANGE_SET_STATUSES = new Set<ArtifactChangeSetStatus>([
  'draft', 'ready', 'applied', 'rejected', 'conflict', 'failed',
])
const ANCHOR_KINDS = new Set<ArtifactAnchorKind>([
  'text_range', 'cell_range', 'slide_shape', 'dom_source', 'generic',
])
const ANCHOR_STATES = new Set<ArtifactAnchorState>(['resolved', 'orphaned'])

const SPREADSHEET_EXTENSIONS = new Set([
  'csv', 'fods', 'ods', 'ots', 'xls', 'xlsb', 'xlsm', 'xlsx', 'xlt', 'xltm', 'xltx',
])
const PRESENTATION_EXTENSIONS = new Set([
  'odp', 'otp', 'pot', 'potm', 'potx', 'pps', 'ppsm', 'ppsx', 'ppt', 'pptm', 'pptx',
])
const HTML_EXTENSIONS = new Set(['htm', 'html', 'xhtml'])

export function objectValue(value: unknown): Record<string, unknown> | null {
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
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function nullableStringAt(raw: Record<string, unknown>, ...keys: string[]): string | null {
  const value = stringAt(raw, ...keys).trim()
  return value || null
}

export function numberAt(raw: Record<string, unknown>, fallback: number, ...keys: string[]): number {
  const parsed = Number(valueAt(raw, ...keys))
  return Number.isFinite(parsed) ? parsed : fallback
}

function timestampAt(
  raw: Record<string, unknown>,
  ...keys: string[]
): number | string | null {
  const value = valueAt(raw, ...keys)
  return typeof value === 'number' || typeof value === 'string' ? value : null
}

function enumAt<T extends string>(
  raw: Record<string, unknown>,
  accepted: ReadonlySet<T>,
  fallback: T,
  ...keys: string[]
): T {
  const value = stringAt(raw, ...keys) as T
  return accepted.has(value) ? value : fallback
}

function extension(name: string): string {
  const normalized = name.trim().toLowerCase()
  const index = normalized.lastIndexOf('.')
  return index >= 0 ? normalized.slice(index + 1) : ''
}

export function artifactDocumentKind(artifact: ArtifactPayload): ArtifactDocumentKind {
  const ext = extension(String(artifact.name || ''))
  const mime = String(artifact.mime || '').split(';', 1)[0].trim().toLowerCase()
  if (mime === 'text/html' || mime === 'application/xhtml+xml' || HTML_EXTENSIONS.has(ext)) {
    return 'html'
  }
  if (SPREADSHEET_EXTENSIONS.has(ext)) return 'spreadsheet'
  if (PRESENTATION_EXTENSIONS.has(ext)) return 'presentation'
  if (isOfficeArtifact(artifact)) return 'document'
  return 'other'
}

function legacyPreviewAvailable(artifact: ArtifactPayload): boolean {
  const ext = extension(String(artifact.name || ''))
  const mime = String(artifact.mime || '').split(';', 1)[0].trim().toLowerCase()
  return mime.startsWith('image/')
    || mime === 'application/pdf'
    || mime === 'text/html'
    || mime === 'application/xhtml+xml'
    || mime === 'text/markdown'
    || mime === 'text/plain'
    || ['htm', 'html', 'xhtml', 'pdf', 'md', 'markdown', 'txt', 'log'].includes(ext)
}

function unavailableEditor(reason: string | null = 'not-supported') {
  return {
    enabled: false,
    preview: false,
    selectionContext: false,
    manualEdit: false,
    agentEdit: false,
    publish: false,
    edit: false,
    comments: false,
    source: false,
    reason,
  }
}

export function unavailableArtifactEditCapabilities(
  reason: string | null = 'capability-unavailable',
): ArtifactEditCapabilities {
  return {
    available: false,
    documents: false,
    revisions: false,
    changeSets: false,
    comments: false,
    context: false,
    office: unavailableEditor(reason),
    html: unavailableEditor(reason),
    reason,
  }
}

function editorCapability(
  value: unknown,
  fallbackReason: string | null,
) {
  if (typeof value === 'boolean') {
    return value
      ? {
          enabled: true,
          preview: true,
          selectionContext: true,
          manualEdit: true,
          agentEdit: true,
          publish: true,
          edit: true,
          comments: true,
          source: false,
          reason: null,
        }
      : unavailableEditor(fallbackReason)
  }
  const raw = objectValue(value)
  if (!raw) return unavailableEditor(fallbackReason)
  const explicitlyEnabled = valueAt(raw, 'enabled', 'available') === true
  const previewValue = valueAt(raw, 'preview', 'canPreview')
  const preview = previewValue === true || (explicitlyEnabled && previewValue !== false)
  const legacyEditValue = valueAt(raw, 'edit', 'canEdit')
  const legacyEdit = legacyEditValue === true
    || (explicitlyEnabled && legacyEditValue !== false)
  const manualEditValue = valueAt(raw, 'manualEdit', 'manual_edit')
  const agentEditValue = valueAt(raw, 'agentEdit', 'agent_edit')
  const manualEdit = manualEditValue === undefined
    ? legacyEdit
    : manualEditValue === true
  const agentEdit = agentEditValue === undefined
    ? legacyEdit
    : agentEditValue === true
  const edit = manualEdit || agentEdit
  const selectionContext = valueAt(
    raw,
    'selectionContext',
    'selection_context',
    'selection',
    'promptAnnotations',
  ) === true
  const publish = valueAt(raw, 'publish') === true
  const source = valueAt(raw, 'source', 'sourcePatch', 'sourceEdit', 'canPatchSource') === true
  const enabled = explicitlyEnabled || preview || edit || selectionContext || source
  return {
    enabled,
    preview,
    selectionContext,
    manualEdit,
    agentEdit,
    publish,
    edit,
    comments: explicitlyEnabled
      ? valueAt(raw, 'comments', 'canComment') !== false
      : valueAt(raw, 'comments', 'canComment') === true,
    source,
    reason: nullableStringAt(raw, 'reason', 'unavailableReason') || (enabled ? null : fallbackReason),
  }
}

export function normalizeArtifactEditCapabilities(value: unknown): ArtifactEditCapabilities {
  const raw = objectValue(value)
  if (!raw) return unavailableArtifactEditCapabilities()
  const formats = objectValue(valueAt(raw, 'formats'))
  if (formats) {
    const html = editorCapability(formats.html, 'html-editor-unavailable')
    const officeFormats = ['docx', 'xlsx', 'pptx']
      .map(format => editorCapability(formats[format], 'office-editor-unavailable'))
    const office = {
      enabled: officeFormats.some(capability => capability.enabled),
      preview: officeFormats.some(capability => capability.preview),
      selectionContext: officeFormats.some(capability => capability.selectionContext),
      manualEdit: officeFormats.some(capability => capability.manualEdit),
      agentEdit: officeFormats.some(capability => capability.agentEdit),
      publish: officeFormats.some(capability => capability.publish),
      edit: officeFormats.some(capability => capability.edit),
      comments: officeFormats.some(capability => capability.comments),
      source: officeFormats.some(capability => capability.source),
      reason: officeFormats.find(capability => capability.reason)?.reason
        || 'office-editor-unavailable',
    }
    return {
      available: true,
      documents: true,
      revisions: true,
      changeSets: true,
      comments: html.comments || office.comments,
      context: true,
      office,
      html,
      reason: null,
    }
  }
  const reason = nullableStringAt(raw, 'reason', 'unavailableReason')
  const office = editorCapability(valueAt(raw, 'office', 'officeEditor'), reason)
  const html = editorCapability(valueAt(raw, 'html', 'htmlEditor'), reason)
  const available = valueAt(raw, 'available', 'enabled') === true
    || office.enabled
    || html.enabled
  return {
    available,
    documents: valueAt(raw, 'documents') !== false && available,
    revisions: valueAt(raw, 'revisions') !== false && available,
    changeSets: valueAt(raw, 'changeSets', 'changes') !== false && available,
    comments: valueAt(raw, 'comments') !== false && available,
    context: valueAt(raw, 'context') !== false && available,
    office,
    html,
    reason: reason || (available ? null : 'capability-unavailable'),
  }
}

function documentCapabilities(
  kind: ArtifactDocumentKind,
  capabilities: ArtifactEditCapabilities,
): ArtifactDocumentCapabilities {
  const editor = kind === 'html'
    ? capabilities.html
    : kind === 'other'
      ? unavailableEditor('editor-unavailable')
      : capabilities.office
  return {
    download: true,
    preview: editor.preview,
    selectionContext: editor.selectionContext,
    manualEdit: editor.manualEdit,
    agentEdit: editor.agentEdit,
    publish: editor.publish,
    edit: editor.edit,
    revisions: capabilities.revisions,
    changeSets: capabilities.changeSets,
    comments: capabilities.comments && editor.comments,
    source: editor.source,
    promptAnnotations: false,
    reason: editor.reason,
  }
}

function documentCapabilitiesFromPayload(
  value: unknown,
  fallback: ArtifactDocumentCapabilities,
): ArtifactDocumentCapabilities {
  const raw = objectValue(value)
  if (!raw) return fallback
  const legacyEdit = valueAt(raw, 'edit') === true
  const manualEditValue = valueAt(raw, 'manualEdit', 'manual_edit')
  const agentEditValue = valueAt(raw, 'agentEdit', 'agent_edit')
  const manualEdit = manualEditValue === undefined
    ? legacyEdit
    : manualEditValue === true
  const agentEdit = agentEditValue === undefined
    ? legacyEdit
    : agentEditValue === true
  return {
    download: valueAt(raw, 'download') !== false,
    preview: valueAt(raw, 'preview') === true,
    selectionContext: valueAt(
      raw,
      'selectionContext',
      'selection_context',
      'selection',
      'promptAnnotations',
    ) === true,
    manualEdit,
    agentEdit,
    publish: valueAt(raw, 'publish') === undefined
      ? fallback.publish
      : valueAt(raw, 'publish') === true,
    edit: manualEdit || agentEdit,
    revisions: valueAt(raw, 'revisions', 'versionHistory') === true,
    changeSets: valueAt(raw, 'changeSets', 'changes') === true
      || valueAt(raw, 'agentEdit') === true,
    comments: valueAt(raw, 'comments') === true,
    source: valueAt(raw, 'source', 'sourceEdit') === true,
    promptAnnotations: valueAt(raw, 'promptAnnotations', 'prompt_annotations') === true,
    reason: nullableStringAt(raw, 'reason', 'unavailableReason'),
  }
}

export function normalizeArtifactDocument(
  value: unknown,
  capabilities = unavailableArtifactEditCapabilities(),
  sessionKey = '',
): ArtifactDocument | null {
  const raw = objectValue(value)
  if (!raw) return null
  const documentId = stringAt(raw, 'documentId', 'document_id', 'id').trim()
  const headRevisionId = stringAt(raw, 'headRevisionId', 'head_revision_id').trim()
  if (!documentId || !headRevisionId) return null
  const format = stringAt(raw, 'format').toLowerCase()
  const kind = format === 'html'
    ? 'html'
    : format === 'xlsx'
      ? 'spreadsheet'
      : format === 'pptx'
        ? 'presentation'
        : format === 'docx'
          ? 'document'
          : enumAt(raw, DOCUMENT_KINDS, 'other', 'kind')
  const fallbackCapabilities = documentCapabilities(kind, capabilities)
  return {
    documentId,
    sessionKey: stringAt(raw, 'sessionKey', 'session_key') || sessionKey,
    sessionId: nullableStringAt(raw, 'sessionId', 'session_id'),
    name: stringAt(raw, 'name') || 'artifact',
    kind,
    headRevisionId,
    latestDownloadUrl: nullableStringAt(raw, 'latestDownloadUrl', 'latest_download_url')
      || `/api/v1/artifact-documents/${encodeURIComponent(documentId)}`,
    generation: Math.max(1, numberAt(raw, 1, 'generation')),
    stateRevision: Math.max(1, numberAt(raw, 1, 'stateRevision', 'state_revision')),
    createdAt: timestampAt(raw, 'createdAt', 'created_at'),
    updatedAt: timestampAt(raw, 'updatedAt', 'updated_at'),
    schemaVersion: Math.max(1, numberAt(raw, 1, 'schemaVersion', 'schema_version')),
    capabilities: documentCapabilitiesFromPayload(
      valueAt(raw, 'capabilities'),
      fallbackCapabilities,
    ),
  }
}

export function normalizeArtifactRevision(value: unknown): ArtifactRevision | null {
  const raw = objectValue(value)
  if (!raw) return null
  const artifact = objectValue(valueAt(raw, 'artifact'))
  const revisionId = stringAt(raw, 'revisionId', 'revision_id', 'id').trim()
  const documentId = stringAt(raw, 'documentId', 'document_id').trim()
  const artifactId = stringAt(raw, 'artifactId', 'artifact_id')
    || (artifact ? stringAt(artifact, 'artifactId', 'artifact_id', 'id') : '')
    || revisionId
  if (!revisionId || !documentId || !artifactId) return null
  return {
    revisionId,
    documentId,
    parentRevisionId: nullableStringAt(raw, 'parentRevisionId', 'parent_revision_id'),
    generation: Math.max(1, numberAt(raw, 1, 'generation')),
    artifactId,
    artifactSha256: stringAt(raw, 'artifactSha256', 'artifact_sha256', 'sha256')
      || (artifact ? stringAt(artifact, 'sha256') : ''),
    filename: stringAt(raw, 'filename', 'name')
      || (artifact ? stringAt(artifact, 'filename', 'name') : '')
      || 'artifact',
    mediaType: stringAt(raw, 'mediaType', 'media_type', 'mime')
      || (artifact ? stringAt(artifact, 'mediaType', 'media_type', 'mime') : ''),
    byteSize: Math.max(0, numberAt(
      raw,
      artifact ? numberAt(artifact, 0, 'byteSize', 'byte_size', 'size') : 0,
      'byteSize',
      'byte_size',
      'size',
    )),
    downloadUrl: nullableStringAt(raw, 'downloadUrl', 'download_url')
      || (artifact ? nullableStringAt(artifact, 'downloadUrl', 'download_url') : null),
    source: enumAt(raw, REVISION_SOURCES, 'initial', 'source'),
    actorKind: enumAt(raw, ACTOR_KINDS, 'system', 'actorKind', 'actor_kind'),
    actorId: stringAt(raw, 'actorId', 'actor_id'),
    changeSetId: nullableStringAt(raw, 'changeSetId', 'change_set_id'),
    copiedFromRevisionId: nullableStringAt(
      raw,
      'copiedFromRevisionId',
      'copied_from_revision_id',
    ),
    createdAt: timestampAt(raw, 'createdAt', 'created_at'),
    schemaVersion: Math.max(1, numberAt(raw, 1, 'schemaVersion', 'schema_version')),
  }
}

export function artifactPayloadFromRevision(revision: ArtifactRevision): ArtifactPayload {
  return {
    id: revision.artifactId,
    name: revision.filename,
    mime: revision.mediaType,
    size: revision.byteSize,
    sha256: revision.artifactSha256,
    ...(revision.downloadUrl ? { download_url: revision.downloadUrl } : {}),
  }
}

function candidateArtifact(raw: Record<string, unknown>): ArtifactPayload | null {
  const nested = objectValue(valueAt(raw, 'candidateArtifact', 'candidate_artifact'))
  const artifactId = nested
    ? stringAt(nested, 'artifactId', 'artifact_id', 'id')
    : stringAt(raw, 'candidateArtifactId', 'candidate_artifact_id')
  if (!artifactId) return null
  return {
    id: artifactId,
    sha256: nested
      ? stringAt(nested, 'sha256')
      : stringAt(raw, 'candidateArtifactSha256', 'candidate_artifact_sha256'),
    name: nested
      ? stringAt(nested, 'filename', 'name')
      : stringAt(raw, 'candidateFilename', 'candidate_filename'),
    mime: nested
      ? stringAt(nested, 'mediaType', 'media_type', 'mime')
      : stringAt(raw, 'candidateMediaType', 'candidate_media_type'),
    size: nested
      ? numberAt(nested, 0, 'byteSize', 'byte_size', 'size')
      : numberAt(raw, 0, 'candidateByteSize', 'candidate_byte_size'),
    ...(nested && nullableStringAt(nested, 'downloadUrl', 'download_url')
      ? { download_url: nullableStringAt(nested, 'downloadUrl', 'download_url')! }
      : {}),
  }
}

export function normalizeArtifactChangeSet(value: unknown): ArtifactChangeSet | null {
  const raw = objectValue(value)
  if (!raw) return null
  const changeSetId = stringAt(raw, 'changeSetId', 'change_set_id', 'id').trim()
  const documentId = stringAt(raw, 'documentId', 'document_id').trim()
  const baseRevisionId = stringAt(raw, 'baseRevisionId', 'base_revision_id').trim()
  if (!changeSetId || !documentId || !baseRevisionId) return null
  const operations = valueAt(raw, 'operations')
  const validation = objectValue(valueAt(raw, 'validation'))
  return {
    changeSetId,
    documentId,
    baseRevisionId,
    turnId: nullableStringAt(raw, 'turnId', 'turn_id'),
    summary: stringAt(raw, 'summary'),
    status: enumAt(raw, CHANGE_SET_STATUSES, 'draft', 'status', 'state'),
    operations: Array.isArray(operations)
      ? operations.map(objectValue).filter((item): item is Record<string, unknown> => !!item)
      : [],
    candidateArtifact: candidateArtifact(raw),
    validation,
    stateRevision: Math.max(1, numberAt(raw, 1, 'stateRevision', 'state_revision')),
    createdByKind: enumAt(
      raw,
      ACTOR_KINDS,
      'system',
      'createdByKind',
      'created_by_kind',
    ),
    createdById: stringAt(raw, 'createdById', 'created_by_id'),
    appliedRevisionId: nullableStringAt(
      raw,
      'appliedRevisionId',
      'applied_revision_id',
      'resultRevisionId',
    ),
    createdAt: timestampAt(raw, 'createdAt', 'created_at'),
    updatedAt: timestampAt(raw, 'updatedAt', 'updated_at'),
    schemaVersion: Math.max(1, numberAt(raw, 1, 'schemaVersion', 'schema_version')),
  }
}

export function normalizeArtifactAnchor(value: unknown): ArtifactAnchor | null {
  const raw = objectValue(value)
  if (!raw) return null
  const anchorId = stringAt(raw, 'anchorId', 'anchor_id').trim()
  const documentId = stringAt(raw, 'documentId', 'document_id').trim()
  const revisionId = stringAt(raw, 'revisionId', 'revision_id').trim()
  if (!anchorId || !documentId || !revisionId) return null
  return {
    anchorId,
    documentId,
    revisionId,
    kind: enumAt(raw, ANCHOR_KINDS, 'generic', 'kind'),
    locator: objectValue(valueAt(raw, 'locator')) || {},
    quote: nullableStringAt(raw, 'quote'),
    context: objectValue(valueAt(raw, 'context')),
    state: enumAt(raw, ANCHOR_STATES, 'resolved', 'state'),
    remappedFromAnchorId: nullableStringAt(
      raw,
      'remappedFromAnchorId',
      'remapped_from_anchor_id',
    ),
    createdAt: timestampAt(raw, 'createdAt', 'created_at'),
    schemaVersion: Math.max(1, numberAt(raw, 1, 'schemaVersion', 'schema_version')),
  }
}

export function normalizeArtifactEditSession(value: unknown): ArtifactEditSession | null {
  const raw = objectValue(value)
  if (!raw) return null
  const editSessionId = stringAt(raw, 'id', 'editSessionId', 'edit_session_id').trim()
  const documentId = stringAt(raw, 'documentId', 'document_id').trim()
  if (!editSessionId || !documentId) return null
  const mode = stringAt(raw, 'mode') === 'edit' ? 'edit' : 'view'
  const statusValue = stringAt(raw, 'status')
  const status = ['active', 'closed', 'expired', 'stale'].includes(statusValue)
    ? statusValue as ArtifactEditSession['status']
    : 'active'
  return {
    editSessionId,
    documentId,
    baseRevisionId: stringAt(raw, 'baseRevisionId', 'base_revision_id'),
    lastSavedRevisionId: stringAt(raw, 'lastSavedRevisionId', 'last_saved_revision_id'),
    mode,
    status,
    stateRevision: Math.max(1, numberAt(raw, 1, 'stateRevision', 'state_revision')),
    expiresAt: timestampAt(raw, 'expiresAt', 'expires_at'),
  }
}

function legacyArtifactIdentity(artifact: ArtifactPayload): string {
  return String(artifact.id || artifact.key || artifact.download_url || artifact.name || 'artifact')
}

export function createLegacyArtifactWorkspace(
  artifact: ArtifactPayload,
  sessionKey: string,
): ArtifactDocumentWorkspace {
  const identity = legacyArtifactIdentity(artifact)
  const documentId = `legacy:${identity}`
  const revisionId = `${documentId}:head`
  const kind = artifactDocumentKind(artifact)
  const office = isOfficeArtifact(artifact)
  const capabilities: ArtifactDocumentCapabilities = {
    download: true,
    preview: !office && legacyPreviewAvailable(artifact),
    selectionContext: false,
    manualEdit: false,
    agentEdit: false,
    publish: false,
    edit: false,
    revisions: false,
    changeSets: false,
    comments: false,
    source: false,
    promptAnnotations: false,
    reason: office ? 'office-editor-unavailable' : 'legacy-read-only',
  }
  const document: ArtifactDocument = {
    documentId,
    sessionKey,
    sessionId: typeof artifact.session_id === 'string' ? artifact.session_id : null,
    name: String(artifact.name || 'artifact'),
    kind,
    headRevisionId: revisionId,
    latestDownloadUrl: typeof artifact.download_url === 'string' ? artifact.download_url : '',
    generation: 1,
    stateRevision: 1,
    createdAt: typeof artifact.createdAt === 'string'
      ? artifact.createdAt
      : typeof artifact.created_at === 'string' ? artifact.created_at : null,
    updatedAt: typeof artifact.createdAt === 'string'
      ? artifact.createdAt
      : typeof artifact.created_at === 'string' ? artifact.created_at : null,
    schemaVersion: 1,
    capabilities,
  }
  const revision: ArtifactRevision = {
    revisionId,
    documentId,
    parentRevisionId: null,
    generation: 1,
    artifactId: String(artifact.id || identity),
    artifactSha256: String(artifact.sha256 || ''),
    filename: String(artifact.name || 'artifact'),
    mediaType: String(artifact.mime || ''),
    byteSize: Math.max(0, Number(artifact.size) || 0),
    downloadUrl: typeof artifact.download_url === 'string' ? artifact.download_url : null,
    source: 'initial',
    actorKind: 'system',
    actorId: '',
    changeSetId: null,
    copiedFromRevisionId: null,
    createdAt: document.createdAt,
    schemaVersion: 1,
  }
  return {
    document,
    revisions: [revision],
    changeSets: [],
    headArtifact: { ...artifact },
    source: 'legacy-artifact',
  }
}
