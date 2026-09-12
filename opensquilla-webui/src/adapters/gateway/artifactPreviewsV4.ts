import type { ArtifactPreviewAccess } from '@/modules/artifactWorkbench'
import { runtimeArtifactBaseOrigin } from './artifactAccessV4'
import {
  createArtifactPreviewLease,
  renewArtifactPreviewLease,
  revokeArtifactPreviewLease,
} from './artifactPreviewLeaseV4'
import { createArtifactPreviewResource } from './artifactPreviewResourceV4'
import { createArtifactPreview } from './artifactPreviewV4'

type ArtifactPreviewHttpTransport = Parameters<typeof createArtifactPreview>[0]
  & Parameters<typeof createArtifactPreviewResource>[0]
  & Parameters<typeof createArtifactPreviewLease>[0]
  & Parameters<typeof renewArtifactPreviewLease>[0]
  & Parameters<typeof revokeArtifactPreviewLease>[0]

interface ArtifactPreviewAdapterOptions {
  baseOrigin?: () => string
}

/** Bind every Artifact preview protocol to the one private HTTP transport. */
export function createV4ArtifactPreviews(
  http: ArtifactPreviewHttpTransport,
  options: ArtifactPreviewAdapterOptions = {},
): ArtifactPreviewAccess {
  const baseOrigin = options.baseOrigin ?? runtimeArtifactBaseOrigin
  return {
    create: request => createArtifactPreview(http, request, baseOrigin),
    createResource: request => createArtifactPreviewResource(http, {
      ...request,
      baseOrigin,
    }),
    createLease: (artifact, mode, client, request = {}) => createArtifactPreviewLease(
      http,
      artifact,
      mode,
      client,
      { ...request, baseOrigin: baseOrigin() },
    ),
    renewLease: (leaseId, request = {}) => renewArtifactPreviewLease(
      http,
      leaseId,
      { ...request, baseOrigin: baseOrigin() },
    ),
    revokeLease: (leaseId, request = {}) => revokeArtifactPreviewLease(
      http,
      leaseId,
      { ...request, baseOrigin: baseOrigin() },
    ),
  }
}
