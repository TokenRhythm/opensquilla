import type { ArtifactPayload } from '@/types/artifacts'
import type { HtmlBundleExportTarget } from '@/modules/artifactWorkbench'
import { artifactWorkbenchPreviewKind } from './artifactPreview'
import { isPreviewPagePath } from './previewPagePath'

export function validExportIdentity(value: unknown, prefix: string): value is string {
  return typeof value === 'string' && value.length <= 256
    && value.startsWith(prefix) && /^[\w-]+$/.test(value) && value.length > prefix.length
}

/** Export identities never come from a download URL or an arbitrary filesystem path. */
export function htmlBundleExportTarget(artifact: ArtifactPayload): HtmlBundleExportTarget | null {
  if (artifactWorkbenchPreviewKind(artifact) !== 'html'
    || artifact.reference?.kind === 'external_url') return null
  const documentId = artifact.documentId ?? artifact.document_id
  if (artifact.source === 'workspace-preview') {
    if (!validExportIdentity(documentId, 'doc_')
      || (artifact.previewPagePath !== undefined && !isPreviewPagePath(artifact.previewPagePath))) return null
    return { type: 'working', documentId,
      ...(typeof artifact.previewPagePath === 'string' ? { pagePath: artifact.previewPagePath } : {}) }
  }
  // Published catalog entries remain immutable even when linked to a document.
  if (artifact.source === 'artifact' && validExportIdentity(artifact.id, 'art-')) {
    return { type: 'artifact', artifactId: artifact.id }
  }
  if (validExportIdentity(documentId, 'doc_')) {
    // A mutable document without a known revision must not silently export a newer head.
    return validExportIdentity(artifact.revisionId, 'rev_')
      ? { type: 'document', documentId, revisionId: artifact.revisionId } : null
  }
  return validExportIdentity(artifact.id, 'art-') ? { type: 'artifact', artifactId: artifact.id } : null
}
