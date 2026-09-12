import type { ArtifactPayload } from '@/types/artifacts'

// Artifact classification remains presentation-safe. HTTP endpoint, header,
// fetch, and open helpers stay private to the Gateway adapters.
function normalizedMime(value: unknown): string {
  return typeof value === 'string' ? value.split(';', 1)[0].trim().toLowerCase() : ''
}

function artifactNameForSafety(artifact: ArtifactPayload): string {
  return typeof artifact.name === 'string' ? artifact.name.trim().toLowerCase() : ''
}

function hasActiveDocumentExtension(artifact: ArtifactPayload): boolean {
  const name = artifactNameForSafety(artifact)
  return name.endsWith('.html') || name.endsWith('.htm') || name.endsWith('.xhtml')
}

export function isActiveDocumentArtifactCandidate(artifact: ArtifactPayload): boolean {
  const artifactMime = normalizedMime(artifact.mime)
  return artifactMime === 'text/html' || artifactMime === 'application/xhtml+xml' ||
    hasActiveDocumentExtension(artifact)
}

export function isActiveDocumentArtifact(artifact: ArtifactPayload, blob: Blob): boolean {
  const responseMime = normalizedMime(blob.type)
  return responseMime === 'text/html' || responseMime === 'application/xhtml+xml' ||
    isActiveDocumentArtifactCandidate(artifact)
}
