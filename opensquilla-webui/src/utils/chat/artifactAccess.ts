// Artifact classification remains presentation-safe. HTTP endpoint, header,
// fetch, and open helpers stay private to the Gateway adapters.
export {
  isActiveDocumentArtifact,
  isActiveDocumentArtifactCandidate,
} from '@/adapters/gateway/artifactAccessV4'
