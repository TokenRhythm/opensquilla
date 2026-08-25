import type {
  ArtifactMutationResolution,
  ArtifactMutationResolutionRequest,
} from '@/types/artifactDocuments'

export const ARTIFACT_MUTATION_RESOLUTION_DELAYS_MS = [0, 250, 1_000, 3_000] as const

type ResolveMutation = (
  request: ArtifactMutationResolutionRequest,
) => Promise<ArtifactMutationResolution | null>

type Wait = (delayMs: number) => Promise<void>

const wait: Wait = delayMs => new Promise(resolve => setTimeout(resolve, delayMs))

/**
 * Perform the bounded outcome check used after a response can no longer prove
 * whether a write was applied. It never replays the write itself.
 */
export async function resolveArtifactMutationBounded(
  resolveMutation: ResolveMutation,
  request: ArtifactMutationResolutionRequest,
  waitFor: Wait = wait,
): Promise<ArtifactMutationResolution | null> {
  let latest: ArtifactMutationResolution | null = null
  for (const delayMs of ARTIFACT_MUTATION_RESOLUTION_DELAYS_MS) {
    if (delayMs > 0) await waitFor(delayMs)
    latest = await resolveMutation(request)
    if (latest === null || latest.status !== 'pending') return latest
  }
  return latest
}
