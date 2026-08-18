import type { ArtifactPayload } from './rpc'

export type ArtifactDocumentKind =
  | 'document'
  | 'spreadsheet'
  | 'presentation'
  | 'html'
  | 'other'

export type ArtifactActorKind = 'user' | 'agent' | 'system'
export type ArtifactRevisionSource =
  | 'initial'
  | 'manual'
  | 'agent'
  | 'restore'
  | 'revert'

export type ArtifactChangeSetStatus =
  | 'draft'
  | 'ready'
  | 'applied'
  | 'rejected'
  | 'conflict'
  | 'failed'

export type ArtifactAnchorKind =
  | 'text_range'
  | 'cell_range'
  | 'slide_shape'
  | 'dom_source'
  | 'generic'

export type ArtifactAnchorState = 'resolved' | 'orphaned'

export interface ArtifactDocumentCapabilities {
  download: boolean
  preview: boolean
  selectionContext: boolean
  manualEdit: boolean
  agentEdit: boolean
  publish: boolean
  /** Compatibility summary for clients predating the independent edit axes. */
  edit: boolean
  revisions: boolean
  changeSets: boolean
  comments: boolean
  source: boolean
  /** Exact source-backed preview annotations advertised for this document head. */
  promptAnnotations?: boolean
  reason: string | null
}

export interface ArtifactEditorCapability {
  enabled: boolean
  preview: boolean
  selectionContext: boolean
  manualEdit: boolean
  agentEdit: boolean
  publish: boolean
  /** Compatibility summary for clients predating the independent edit axes. */
  edit: boolean
  comments: boolean
  source: boolean
  reason: string | null
}

/** Server-advertised editor capabilities. The default is deliberately deny-by-default. */
export interface ArtifactEditCapabilities {
  available: boolean
  documents: boolean
  revisions: boolean
  changeSets: boolean
  comments: boolean
  context: boolean
  office: ArtifactEditorCapability
  html: ArtifactEditorCapability
  reason: string | null
}

export interface ArtifactDocument {
  documentId: string
  sessionKey: string
  sessionId: string | null
  name: string
  kind: ArtifactDocumentKind
  headRevisionId: string
  /** Stable endpoint that resolves the current head at request time. */
  latestDownloadUrl: string
  generation: number
  stateRevision: number
  createdAt: number | string | null
  updatedAt: number | string | null
  schemaVersion: number
  capabilities: ArtifactDocumentCapabilities
}

export interface ArtifactRevision {
  revisionId: string
  documentId: string
  parentRevisionId: string | null
  generation: number
  artifactId: string
  artifactSha256: string
  filename: string
  mediaType: string
  byteSize: number
  downloadUrl: string | null
  source: ArtifactRevisionSource
  actorKind: ArtifactActorKind
  actorId: string
  changeSetId: string | null
  copiedFromRevisionId: string | null
  createdAt: number | string | null
  schemaVersion: number
}

export interface ArtifactChangeSet {
  changeSetId: string
  documentId: string
  baseRevisionId: string
  turnId: string | null
  summary: string
  status: ArtifactChangeSetStatus
  operations: ReadonlyArray<Readonly<Record<string, unknown>>>
  candidateArtifact: ArtifactPayload | null
  validation: Readonly<Record<string, unknown>> | null
  stateRevision: number
  createdByKind: ArtifactActorKind
  createdById: string
  appliedRevisionId: string | null
  createdAt: number | string | null
  updatedAt: number | string | null
  schemaVersion: number
}

export interface ArtifactAnchor {
  anchorId: string
  documentId: string
  revisionId: string
  kind: ArtifactAnchorKind
  locator: Readonly<Record<string, unknown>>
  quote: string | null
  context: Readonly<Record<string, unknown>> | null
  state: ArtifactAnchorState
  remappedFromAnchorId: string | null
  createdAt: number | string | null
  schemaVersion: number
}

export interface ArtifactEditSession {
  editSessionId: string
  documentId: string
  baseRevisionId: string
  lastSavedRevisionId: string
  mode: 'view' | 'edit'
  status: 'active' | 'closed' | 'expired' | 'stale'
  stateRevision: number
  expiresAt: number | string | null
}

export interface ArtifactEditSessionStartRequest {
  sessionKey: string
  documentId: string
  mode: 'edit'
  clientRequestId?: string
}

export interface ArtifactEditSessionHeartbeatRequest {
  sessionKey: string
  editSessionId: string
  expectedStateRevision: number
}

export interface ArtifactEditSessionCloseRequest {
  sessionKey: string
  editSessionId: string
  expectedStateRevision: number
}

export interface ArtifactSourceSnapshot {
  documentId: string
  revisionId: string
  language: string
  content: string
  sha256: string
  /** All source RPC offsets use Unicode code points, never JavaScript UTF-16 units. */
  offsetEncoding: 'unicode-code-point'
  patchCount: number | null
  stateRevision: number
}

export interface ArtifactSourcePatchResult extends ArtifactSourceSnapshot {
  /** Present when the patch was committed through a durable EditSession. */
  editSession: ArtifactEditSession | null
}

export type ArtifactMutationOperation =
  | 'source.patch'
  | 'revision.restore'
  | 'change.revert'
  | 'document.import'
  | 'document.publish'
  | 'workbench.resources.open'

export interface ArtifactMutationResolutionRequest {
  sessionKey: string
  operation: ArtifactMutationOperation
  requestId: string
  documentId?: string
}

export interface ArtifactMutationResolutionResult {
  documentId: string
  revisionId: string
  sha256: string
  stateRevision: number
}

export interface ArtifactMutationResolution {
  status: 'applied' | 'not_applied' | 'pending'
  retryAfterMs: number | null
  result: ArtifactMutationResolutionResult | null
  /** Canonical read model returned after a durable applied outcome. */
  document?: ArtifactDocument | null
  revision?: ArtifactRevision | null
}

export type ArtifactDocumentWorkspaceSource = 'document-api' | 'legacy-artifact'

export interface ArtifactDocumentWorkspace {
  document: ArtifactDocument
  revisions: readonly ArtifactRevision[]
  changeSets: readonly ArtifactChangeSet[]
  headArtifact: ArtifactPayload
  source: ArtifactDocumentWorkspaceSource
}

export interface ArtifactDocumentWorkspaceSnapshot {
  key: string
  loading: boolean
  loaded: boolean
  /** The workspace is the last successfully loaded value after a refresh failure. */
  stale: boolean
  error: string | null
  workspace: ArtifactDocumentWorkspace | null
}

/**
 * Capability-scoped mutations exposed to the artifact document panel.
 *
 * Callers provide the stable artifact/session identity only. Implementations
 * must derive document IDs and optimistic-concurrency values from the latest
 * loaded workspace instead of accepting those security-sensitive fields from
 * the view.
 */
export interface ArtifactDocumentActions {
  restoreRevision(
    artifact: ArtifactPayload,
    sessionKey: string,
    revisionId: string,
  ): Promise<ArtifactDocumentWorkspace>
  revertChangeSet(
    artifact: ArtifactPayload,
    sessionKey: string,
    changeSetId: string,
  ): Promise<ArtifactDocumentWorkspace>
}

export interface ArtifactDocumentsListResponse {
  documents?: unknown[]
}

export interface ArtifactDocumentResponse {
  document?: unknown
}

export interface ArtifactRevisionsListResponse {
  revisions?: unknown[]
}

export interface ArtifactChangeSetsListResponse {
  changeSets?: unknown[]
}

export interface ArtifactChangeSetResponse {
  changeSet?: unknown
}

export interface ArtifactEditCapabilitiesResponse {
  capabilities?: unknown
  formats?: unknown
}
