import type { ArtifactPayload } from '@/types/artifacts'

/** File actions beside a preview refer to that revision, not a later head. */
export function artifactPreviewActionTarget(artifact: ArtifactPayload): ArtifactPayload {
  const revisionUrl = artifact.revision_download_url
  return typeof revisionUrl === 'string' && revisionUrl.trim()
    ? { ...artifact, download_url: revisionUrl }
    : { ...artifact }
}
