// Compatibility exports for pure helpers and focused contract tests.
// Production network access is composed through ArtifactWorkbench.
export {
  artifactAccessHeaders,
  artifactAccessUrl,
  artifactGatewayOpenUrl,
  artifactOpenFailureMessage,
  fetchArtifactBlob,
  isActiveDocumentArtifact,
  isActiveDocumentArtifactCandidate,
  isSameOriginArtifactUrl,
  isTrustedArtifactTransportUrl,
  openArtifactBlobUrl,
  openArtifactViaGateway,
} from '@/adapters/gateway/artifactAccessV4'
export type {
  ArtifactAuthContext,
  ArtifactFetchOptions,
  ArtifactFetchResult,
  ArtifactGatewayOpenResult,
  ArtifactOpenOptions,
  ArtifactOpenResult,
} from '@/adapters/gateway/artifactAccessV4'
