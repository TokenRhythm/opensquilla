export type { ArtifactDocumentProvider } from '@/modules/artifactWorkbench'
export {
  artifactDocumentKind,
  artifactPayloadFromRevision,
  createLegacyArtifactWorkspace,
  isOfficeArtifact,
  normalizeArtifactAnchor,
  normalizeArtifactChangeSet,
  normalizeArtifactDocument,
  normalizeArtifactEditCapabilities,
  normalizeArtifactEditSession,
  normalizeArtifactRevision,
  unavailableArtifactEditCapabilities,
} from '@/adapters/gateway/artifactDocumentsV4'
