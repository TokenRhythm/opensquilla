import type { ArtifactDocument, ArtifactRevision } from './artifactDocuments'
import type { ArtifactPayload } from './rpc'

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
  /** Legal user-facing edit entry; immutable sources use explicit copy-import. */
  manualEdit: boolean
  agentEdit: boolean
  /** Compatibility summary for clients predating the independent edit axes. */
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
  size?: number
  sha256?: string
  createdAt?: number | string | null
  updatedAt?: number | string | null
  downloadUrl?: string
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
  mode: 'isolated'
  resource: WorkbenchResourceRef
  launchUrl?: string
  sandboxProfile: 'opaque-offline'
  network: false
  adapter?: Readonly<Record<string, unknown>> | null
}

export interface WorkbenchPreviewResponse {
  resource: WorkbenchResource
  preview: WorkbenchPreviewDescriptor
}

/**
 * Resolves the resource a user clicked to its current editable identity. The
 * Gateway may materialize an old immutable HTML source, but that copy boundary
 * is deliberately hidden from the Workbench interaction.
 */
export type WorkbenchResourceOpenResponse =
  | {
      disposition: 'document'
      resolution: { status: 'current' | 'materialized' }
      resource: WorkbenchResource
      document: ArtifactDocument
      revision: ArtifactRevision
      binding?: DocumentSourceBinding
      materialized: boolean
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
