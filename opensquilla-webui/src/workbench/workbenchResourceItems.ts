import type { ArtifactPayload } from '@/types/rpc'
import type {
  WorkbenchPreviewResponse,
  WorkbenchResource,
  WorkbenchResourceRef,
} from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import type { WorkbenchItem } from './types'

function identityToken(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let hash = 0x811c9dc5
  for (const byte of bytes) {
    hash ^= byte
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return `${bytes.length.toString(36)}-${hash.toString(16).padStart(8, '0')}`
}

export function workbenchResourceKey(resource: WorkbenchResourceRef): string {
  return `${resource.type}:${workbenchResourceRefId(resource)}`
}

export function resourceCollectionWorkbenchItemId(sessionKey: string): string {
  return `resource-collection:${identityToken(sessionKey)}`
}

export function createResourceCollectionWorkbenchItem(options: {
  resources: readonly WorkbenchResource[]
  sessionKey: string
  title: string
}): WorkbenchItem {
  return {
    id: resourceCollectionWorkbenchItemId(options.sessionKey),
    kind: 'resource-collection',
    title: options.title,
    scope: { type: 'session', id: options.sessionKey },
    hostKind: 'dom',
    retention: 'keep-alive',
    payload: {
      resources: [...options.resources],
      sessionKey: options.sessionKey,
    },
  }
}

export function resourcesFromWorkbenchItem(
  item: WorkbenchItem | null,
): readonly WorkbenchResource[] {
  if (item?.kind !== 'resource-collection') return []
  const resources = item.payload.resources
  return Array.isArray(resources)
    ? resources.filter((resource): resource is WorkbenchResource => Boolean(
        resource
        && typeof resource === 'object'
        && 'resource' in resource,
      ))
    : []
}

/**
 * Adapts a read-only Workbench projection to the existing preview renderer.
 * The resource identity remains separate from the ArtifactStore identity;
 * callers must pass it to createArtifactPreviewWorkbenchItem.
 */
export function artifactPayloadFromWorkbenchResource(
  resource: WorkbenchResource,
): ArtifactPayload {
  const isDocument = resource.resource.type === 'document'
  const headArtifactId = isDocument
    ? resource.relations.headArtifactId
    : undefined
  const artifactId = resource.resource.type === 'deliverable'
    ? workbenchResourceRefId(resource.resource)
    : headArtifactId
  return {
    ...(artifactId ? { id: artifactId } : {}),
    name: resource.name,
    mime: resource.mime,
    size: resource.size,
    sha256: resource.sha256,
    ...(resource.downloadUrl ? { download_url: resource.downloadUrl } : {}),
    ...(isDocument && resource.relations.documentId
      ? { documentId: resource.relations.documentId }
      : {}),
    ...(isDocument && resource.relations.headRevisionId
      ? { revisionId: resource.relations.headRevisionId }
      : {}),
    workbenchResourceType: resource.resource.type,
    workbenchResourceId: workbenchResourceRefId(resource.resource),
  }
}

/**
 * Bind the server-validated launch target to the existing isolated preview
 * renderer without mutating the durable resource projection.
 */
export function resourceFromPreparedPreview(
  response: WorkbenchPreviewResponse,
): WorkbenchResource {
  const launchUrl = response.preview.launchUrl
  return launchUrl
    ? { ...response.resource, downloadUrl: launchUrl }
    : response.resource
}
