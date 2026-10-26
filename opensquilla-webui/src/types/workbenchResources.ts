import type { ChatPageContext } from './pageContext'
import type { ArtifactDocument, ArtifactRevision } from './artifactDocuments'
import type { ArtifactPayload } from './artifacts'

export type WorkbenchResourceType = 'attachment' | 'document' | 'deliverable' | 'url'

type CompatibleResourceIdentity<CanonicalKey extends string> =
  | ({ id: string } & Partial<Record<CanonicalKey, string>>)
  | ({ id?: string } & Record<CanonicalKey, string>)

/**
 * Public discriminated Workbench identity. Canonical callers use the
 * type-specific ID; the generic `id` spelling remains an additive V036 alias.
 */
export type WorkbenchResourceRef =
  | ({ type: 'attachment' } & CompatibleResourceIdentity<'attachmentId'>)
  | ({ type: 'document' } & CompatibleResourceIdentity<'documentId'>)
  | ({ type: 'deliverable' } & CompatibleResourceIdentity<'artifactId'>)
  | ({ type: 'url' } & CompatibleResourceIdentity<'urlId'>)

export function createWorkbenchResourceRef(
  type: WorkbenchResourceType,
  id: string,
): WorkbenchResourceRef {
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

export function workbenchResourceRefId(resource: WorkbenchResourceRef): string {
  switch (resource.type) {
    case 'attachment':
      return resource.attachmentId || resource.id || ''
    case 'document':
      return resource.documentId || resource.id || ''
    case 'deliverable':
      return resource.artifactId || resource.id || ''
    case 'url':
      return resource.urlId || resource.id || ''
  }
}

export interface WorkbenchResourceCapabilities {
  preview: boolean
  download: boolean
  selectionContext: boolean
  /** Legacy source-editor capability, retained for older resource projections. */
  manualEdit: boolean
  agentEdit: boolean
  /** General editing capability; immutable sources use a working copy. */
  edit: boolean
  publish: boolean
  previewReasonCode?: string | null
  editReasonCode?: string | null
  /** Compatibility summary for early resource projections. */
  reasonCode?: string | null
}

export interface WorkbenchResourceRelations {
  documentId?: string
  headRevisionId?: string
  headArtifactId?: string
  source?: WorkbenchResourceRef
  deliverableId?: string
  publishedRevisionId?: string
}

/**
 * A read-only projection used by the Workbench. The underlying Attachment,
 * Document and Deliverable retain independent storage and lifecycle rules.
 */
export interface WorkbenchResource {
  resource: WorkbenchResourceRef
  name: string
  mime: string
  /** Authorized workspace-relative source; presentation only, never an identity. */
  sourcePath?: string
  /** Product-facing origin, e.g. the persisted inline Visualize widget. */
  source?: string
  /** Storage/projection visibility. Internal widgets stay out of Files. */
  visibility?: 'listed' | 'internal' | string
  /** Presentation surface requested by the producer, e.g. inline. */
  presentation?: string
  size?: number
  sha256?: string
  createdAt?: number | string | null
  updatedAt?: number | string | null
  downloadUrl?: string
  /** HTML members of the current registered bundle; opening revalidates access. */
  previewPages?: string[]
  capabilities: WorkbenchResourceCapabilities
  relations: WorkbenchResourceRelations
}

export interface WorkbenchResourcesListResponse {
  resources: WorkbenchResource[]
  totalCount: number
  nextCursor?: string | null
  hasMore?: boolean
}

export interface WorkbenchPreviewDescriptor {
  protocolVersion: number
  mode: 'isolated' | 'local-renderer'
  resource: WorkbenchResourceRef
  launchUrl?: string
  sandboxProfile: 'opaque-offline' | 'none'
  /** Document external resources are disabled; local-renderer is not an origin sandbox. */
  network: false
  adapter?: Readonly<Record<string, unknown>> | null
}

export interface WorkbenchPreviewResponse {
  resource: WorkbenchResource
  preview: WorkbenchPreviewDescriptor
}
export type WorkbenchAnnotationRequest = (
  | { resourceRef: WorkbenchResourceRef; workspaceFile?: never; expectedSha256?: string }
  | { resourceRef?: never; workspaceFile: { path: string; workspaceBinding: string; expectedRevision: string }; expectedSha256?: never }
) & { idempotencyKey?: string }

/**
 * Resolves the resource a user clicked to its current editable identity. The
 * Gateway may materialize an old immutable HTML source, but that copy boundary
 * is deliberately hidden from the Workbench interaction.
 */
export type WorkbenchResourceOpenResponse =
  | {
      disposition: 'document'
      pageContext?: ChatPageContext
      workingFile?: string
      resolution: { status: 'current' | 'materialized' }
      resource: WorkbenchResource
      document: ArtifactDocument
      revision: ArtifactRevision
      binding?: DocumentSourceBinding
      materialized: boolean
      baselineUpdated?: boolean
      previousRevisionId?: string
    }
  | {
      disposition: 'readonly'
      resolution: { status: 'readonly' }
      resource: WorkbenchResource
      reasonCode: string
      materialized: false
    }

export interface DocumentSourceBinding {
  bindingId: string
  documentId: string
  source: WorkbenchResourceRef
  sourceSha256: string
  mode: 'copy'
}

export interface DocumentOperationReceipt {
  attemptId: string
  requestId: string
  /** Compatibility alias retained for initial V036 gateways. */
  idempotencyKey: string
  status: 'applied' | 'failed' | 'ambiguous'
  replayed: boolean
  failureCode?: string | null
}

export interface DocumentImportResponse {
  document: ArtifactDocument
  revision: ArtifactRevision
  binding?: DocumentSourceBinding
  receipt?: DocumentOperationReceipt
}

export interface DocumentPublication {
  publicationId: string
  documentId: string
  revisionId: string
  artifactId: string
  createdAt?: number | string | null
}

export interface DocumentPublishResponse {
  deliverable: ArtifactPayload
  publication: DocumentPublication
  receipt: DocumentOperationReceipt
}
