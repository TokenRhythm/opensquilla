import type { ArtifactPayload } from '@/types/artifacts'

/** JSON-only hints for a previously authorized, revision-matched resource. */
export interface ResourceReadingState {
  page: number
  mode: 'width' | 'page' | 'manual'
  scale: number
  top: number
  left: number
  time: number
}

export function resourceReadingState(value: unknown): ResourceReadingState | undefined {
  if (!value || typeof value !== 'object') return undefined
  const state = value as Record<string, unknown>
  const number = (key: string, fallback: number, maximum: number) =>
    typeof state[key] === 'number' && Number.isFinite(state[key])
      ? Math.max(0, Math.min(maximum, state[key])) : fallback
  return {
    page: Math.floor(number('page', 0, 1_000_000)),
    mode: state.mode === 'page' || state.mode === 'manual' ? state.mode : 'width',
    scale: Math.max(0.1, number('scale', 1, 10)),
    top: number('top', 0, 10_000_000),
    left: number('left', 0, 10_000_000),
    time: number('time', 0, 31_536_000),
  }
}
export function artifactReadingIdentity(artifact: ArtifactPayload, sessionKey: string): string {
  return JSON.stringify([sessionKey, artifact.documentId || artifact.id || artifact.key || '',
    artifact.revisionId || artifact.sha256 || artifact.revision_download_url || artifact.download_url || '',
    artifact.size || 0])
}

export interface ArtifactReadingCheckpoint { identity: string; reading: ResourceReadingState }

export function artifactReadingCheckpoint(value: unknown): ArtifactReadingCheckpoint | undefined {
  if (!value || typeof value !== 'object') return undefined
  const saved = value as Record<string, unknown>
  const reading = resourceReadingState(saved.reading)
  return typeof saved.identity === 'string' && saved.identity.length <= 8192 && reading
    ? { identity: saved.identity, reading } : undefined
}
