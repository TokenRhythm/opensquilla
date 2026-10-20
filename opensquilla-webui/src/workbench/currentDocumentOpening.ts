import type { WorkbenchResource, WorkbenchResourceRef } from '@/types/workbenchResources'
import { createWorkbenchResourceRef } from '@/types/workbenchResources'
import { artifactProductClientError } from '@/utils/artifactProductErrors'
import type { ArtifactPayload } from '@/types/artifacts'
import type { ArtifactDocumentWorkspace, ArtifactDocumentWorkspaceSnapshot } from '@/types/artifactDocuments'
import { artifactPayloadFromWorkbenchResource } from './workbenchResourceItems'

interface CurrentDocumentSnapshots {
  snapshot(artifact: ArtifactPayload, sessionKey: string): ArtifactDocumentWorkspaceSnapshot
  load(artifact: ArtifactPayload, sessionKey: string, options?: { force?: boolean }): Promise<ArtifactDocumentWorkspace>
}

/**
 * Normal navigation follows an authorized, explicit Document relation. This
 * deliberately uses discovery only: viewing a delivery never imports a copy
 * or absorbs external edits into a new revision.
 */
export async function resolveCurrentDocumentResource(
  resolve: (sessionKey: string, ref: WorkbenchResourceRef) => Promise<WorkbenchResource | null>,
  sessionKey: string,
  ref: WorkbenchResourceRef,
  isCurrent: () => boolean = () => true,
): Promise<WorkbenchResource | null> {
  const resource = await resolve(sessionKey, ref)
  if (!isCurrent()) return null
  if (!resource || resource.resource.type === 'document') return resource
  const documentId = resource.relations.documentId
  if (!documentId) return resource
  const current = await resolve(sessionKey, createWorkbenchResourceRef('document', documentId))
  if (!isCurrent()) return null
  // An unavailable current document must not silently look like its old copy.
  if (!current) throw artifactProductClientError('RESOURCE_UNSUPPORTED')
  return current
}

/**
 * The resolved head is authoritative for navigation. A cached preview from a
 * previous visit may only be reused when it describes that same revision.
 */
export async function prepareCurrentDocumentPreview(
  resource: WorkbenchResource,
  sessionKey: string,
  documents: CurrentDocumentSnapshots,
  isCurrent: () => boolean = () => true,
): Promise<ArtifactPayload | null> {
  if (!isCurrent()) return null
  const artifact = artifactPayloadFromWorkbenchResource(resource)
  if (resource.resource.type !== 'document') return artifact
  const documentId = resource.resource.documentId || resource.resource.id
  const expectedRevision = resource.relations.headRevisionId
  let snapshot = documents.snapshot(artifact, sessionKey)
  if (!snapshot.loaded || snapshot.loading || snapshot.stale || snapshot.error
    || snapshot.workspace?.source !== 'document-api'
    || snapshot.workspace.document.headRevisionId !== expectedRevision) {
    await documents.load(artifact, sessionKey, { force: true })
    if (!isCurrent()) return null
    snapshot = documents.snapshot(artifact, sessionKey)
  }
  const workspace = snapshot.workspace
  if (!snapshot.loaded || snapshot.loading || snapshot.stale || snapshot.error
    || workspace?.source !== 'document-api' || workspace.document.documentId !== documentId) {
    throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
  }
  return {
    ...artifact,
    ...workspace.headArtifact,
    documentId,
    revisionId: workspace.document.headRevisionId,
    ...(resource.sourcePath ? { sourcePath: resource.sourcePath } : {}),
  }
}
