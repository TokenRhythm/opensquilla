export type { ArtifactDocumentProvider } from '@/modules/artifactWorkbench'
export {
  ARTIFACT_DOCUMENT_RPC_METHODS,
  artifactDocumentKind,
  artifactPayloadFromRevision,
  createLegacyArtifactWorkspace,
  createRpcArtifactDocumentProvider,
  isOfficeArtifact,
  normalizeArtifactAnchor,
  normalizeArtifactChangeSet,
  normalizeArtifactDocument,
  normalizeArtifactEditCapabilities,
  normalizeArtifactEditSession,
  normalizeArtifactRevision,
  unavailableArtifactEditCapabilities,
} from '@/adapters/gateway/artifactDocumentsV4'
