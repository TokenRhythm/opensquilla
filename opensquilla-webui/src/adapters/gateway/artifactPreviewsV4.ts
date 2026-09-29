import {
  ArtifactPreviewLeaseError,
  type ArtifactPreviewAccess,
  type ArtifactPreviewLeaseRequest,
} from '@/modules/artifactWorkbench'
import { runtimeArtifactBaseOrigin } from './artifactAccessV4'
import {
  createArtifactPreviewLease,
  renewArtifactPreviewLease,
  revokeArtifactPreviewLease,
} from './artifactPreviewLeaseV4'
import { createArtifactPreviewResource } from './artifactPreviewResourceV4'
import { createArtifactPreview } from './artifactPreviewV4'

type ArtifactPreviewHttpTransport = Parameters<typeof createArtifactPreview>[0]
  & Parameters<typeof createArtifactPreviewResource>[0]
  & Parameters<typeof createArtifactPreviewLease>[0]
  & Parameters<typeof renewArtifactPreviewLease>[0]
  & Parameters<typeof revokeArtifactPreviewLease>[0]

interface ArtifactPreviewAdapterOptions {
  baseOrigin?: () => string
}

interface PendingWebRevocation {
  key: string
  leaseId: string
  origin: string
  sessionKey?: string
  failures: number
  timer: ReturnType<typeof setTimeout> | null
  inFlight?: Promise<void>
}

/** Bind every Artifact preview protocol to the one private HTTP transport. */
export function createV4ArtifactPreviews(
  http: ArtifactPreviewHttpTransport,
  options: ArtifactPreviewAdapterOptions = {},
): ArtifactPreviewAccess {
  const baseOrigin = options.baseOrigin ?? runtimeArtifactBaseOrigin
  // Cleanup outlives the panel that requested it. These records grant no
  // preview authority and retain no credentials; HTTP reads current auth.
  const pendingRevocations = new Map<string, PendingWebRevocation>()
  const revokeHttp = {
    requestBlob: (...[endpoint, request]: Parameters<ArtifactPreviewHttpTransport['requestBlob']>) =>
      http.requestBlob(endpoint, { ...request, timeoutMs: 15_000 }),
  }

  function scheduleRevocation(task: PendingWebRevocation, delay: number) {
    if (task.timer !== null) clearTimeout(task.timer)
    task.timer = setTimeout(() => {
      task.timer = null
      void attemptRevocation(task).catch(() => undefined)
    }, delay)
  }

  function attemptRevocation(task: PendingWebRevocation): Promise<void> {
    if (task.inFlight) return task.inFlight
    task.inFlight = Promise.resolve().then(async () => {
      try {
        // Never redirect old cleanup or its session identity to a new Gateway.
        if (baseOrigin() !== task.origin) {
          scheduleRevocation(task, 300_000)
          return
        }
        await revokeArtifactPreviewLease(revokeHttp, task.leaseId, {
          baseOrigin: task.origin,
          sessionKey: task.sessionKey,
        })
        pendingRevocations.delete(task.key)
      } catch (error) {
        if (error instanceof ArtifactPreviewLeaseError && error.retryable) {
          task.failures = Math.min(task.failures + 1, 4)
          const ceiling = Math.min(300_000, 60_000 * 2 ** (task.failures - 1))
          scheduleRevocation(task, ceiling * (0.75 + Math.random() * 0.25))
        } else if (
          error instanceof ArtifactPreviewLeaseError
          && (error.status === 401 || error.status === 403)
        ) {
          // Auth may recover while this app stays open. Avoid a tight loop and
          // let the transport supply fresh credentials on the next attempt.
          scheduleRevocation(task, 300_000)
        }
        // Other failures remain dormant for an explicit retry, not forgotten.
        // No age-based expiry: resource reads can extend the server lease.
        throw error
      } finally {
        task.inFlight = undefined
      }
    })
    return task.inFlight
  }

  function revokeLease(leaseId: string, request: ArtifactPreviewLeaseRequest = {}): Promise<void> {
    const origin = baseOrigin()
    // Desktop owns its pending cleanup in the main process, including reloads.
    if (request.nativeBroker) {
      return revokeArtifactPreviewLease(http, leaseId, { ...request, baseOrigin: origin })
    }
    const key = JSON.stringify([origin, request.sessionKey || '', leaseId])
    let task = pendingRevocations.get(key)
    if (!task) {
      task = { key, leaseId, origin, sessionKey: request.sessionKey, failures: 0, timer: null }
      pendingRevocations.set(key, task)
    }
    if (task.timer !== null) {
      clearTimeout(task.timer)
      task.timer = null
    }
    return attemptRevocation(task)
  }

  return {
    create: request => createArtifactPreview(http, request, baseOrigin),
    createResource: request => createArtifactPreviewResource(http, {
      ...request,
      baseOrigin,
    }),
    createLease: (artifact, mode, client, request = {}) => createArtifactPreviewLease(
      http,
      artifact,
      mode,
      client,
      { ...request, baseOrigin: baseOrigin() },
    ),
    renewLease: (leaseId, request = {}) => renewArtifactPreviewLease(
      http,
      leaseId,
      { ...request, baseOrigin: baseOrigin() },
    ),
    revokeLease,
  }
}
