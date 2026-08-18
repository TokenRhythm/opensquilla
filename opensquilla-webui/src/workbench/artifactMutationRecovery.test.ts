import { describe, expect, it, vi } from 'vitest'

import type { ArtifactMutationResolution } from '@/types/artifactDocuments'
import {
  ARTIFACT_MUTATION_RESOLUTION_DELAYS_MS,
  resolveArtifactMutationBounded,
} from './artifactMutationRecovery'

const request = {
  sessionKey: 'session-a',
  operation: 'source.patch' as const,
  requestId: 'request-a',
  documentId: 'document-a',
}

describe('resolveArtifactMutationBounded', () => {
  it('checks immediately and then at the bounded recovery intervals', async () => {
    const pending: ArtifactMutationResolution = {
      status: 'pending', retryAfterMs: 250, result: null,
    }
    const applied: ArtifactMutationResolution = {
      status: 'applied',
      retryAfterMs: null,
      result: {
        documentId: 'document-a',
        revisionId: 'revision-b',
        sha256: 'b'.repeat(64),
        stateRevision: 2,
      },
    }
    const resolveMutation = vi.fn()
      .mockResolvedValueOnce(pending)
      .mockResolvedValueOnce(pending)
      .mockResolvedValueOnce(applied)
    const waits: number[] = []

    await expect(resolveArtifactMutationBounded(
      resolveMutation,
      request,
      async delay => { waits.push(delay) },
    )).resolves.toEqual(applied)
    expect(waits).toEqual([250, 1_000])
  })

  it('stops after 0/250/1000/3000ms and does not replay a write', async () => {
    const pending: ArtifactMutationResolution = {
      status: 'pending', retryAfterMs: 250, result: null,
    }
    const resolveMutation = vi.fn().mockResolvedValue(pending)
    const waits: number[] = []

    await expect(resolveArtifactMutationBounded(
      resolveMutation,
      request,
      async delay => { waits.push(delay) },
    )).resolves.toEqual(pending)
    expect(resolveMutation).toHaveBeenCalledTimes(4)
    expect(waits).toEqual(ARTIFACT_MUTATION_RESOLUTION_DELAYS_MS.slice(1))
  })

  it('returns null immediately for an older Gateway', async () => {
    const resolveMutation = vi.fn().mockResolvedValue(null)
    const wait = vi.fn()

    await expect(resolveArtifactMutationBounded(resolveMutation, request, wait))
      .resolves.toBeNull()
    expect(wait).not.toHaveBeenCalled()
  })
})
