import type { ArtifactPayload } from '@/types/rpc'
import type {
  WorkbenchPreviewResponse,
  WorkbenchResource,
  WorkbenchResourceRef,
} from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import { artifactWorkbenchPreviewKind } from '@/utils/workbench/artifactPreview'
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

/**
 * Collapse only server-declared resource lineages for navigation. Raw resource
 * identities remain available to transcript cards, downloads and audit views.
 * Names and content hashes are intentionally ignored: two unrelated files may
 * legitimately share either value.
 */
export function canonicalWorkbenchResources(
  resources: readonly WorkbenchResource[],
): WorkbenchResource[] {
  const documentIds = new Set(
    resources
      .filter(resource => resource.resource.type === 'document')
      .map(resource => workbenchResourceRefId(resource.resource)),
  )
  return resources.filter((resource) => {
    if (resource.resource.type === 'document') return true
    const documentId = resource.relations.documentId || ''
    return !documentId || !documentIds.has(documentId)
  })
}

export function resourceCollectionWorkbenchItemId(sessionKey: string): string {
  return `resource-collection:${identityToken(sessionKey)}`
}

export function createResourceCollectionWorkbenchItem(options: {
  resources: readonly WorkbenchResource[]
  sessionKey: string
  title: string
}): WorkbenchItem {
  const resources = canonicalWorkbenchResources(options.resources)
  return {
    id: resourceCollectionWorkbenchItemId(options.sessionKey),
    kind: 'resource-collection',
    title: options.title,
    scope: { type: 'session', id: options.sessionKey },
    hostKind: 'dom',
    retention: 'keep-alive',
    payload: {
      resources,
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
 * Desktop can give artifact-backed HTML resources a full/offline preview
 * switch without exposing host credentials, files, Node, or Electron APIs.
 * Attachments have no ArtifactStore preview lease and stay on the prepared
 * opaque-offline path.
 */
export function resourceUsesNativeHtmlPreview(resource: WorkbenchResource): boolean {
  const artifactBacked = resource.resource.type === 'deliverable'
    || Boolean(
      resource.resource.type === 'document'
      && resource.relations.documentId
      && resource.relations.headArtifactId,
    )
  return artifactBacked
    && artifactWorkbenchPreviewKind(artifactPayloadFromWorkbenchResource(resource)) === 'html'
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
