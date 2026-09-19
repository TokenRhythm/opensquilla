import { WORKSPACES_REFERENCES_READ_METHOD } from '@/contracts/generated/v4/workspacesReferencesRead'
import {
  validateParams as validateWorkspacesReferencesReadParams,
  validateResult as validateWorkspacesReferencesReadResult,
} from '@/contracts/generated/v4/workspacesReferencesReadValidators.mjs'
import type { WorkspaceReferences, WorkspaceSourceSnapshot } from '@/modules/workspaceReferences'
import { WorkspaceReferenceError } from '@/modules/workspaceReferences'
import { normalizeWorkspaceFileReferenceV1 } from '@/types/references'
import { readTransportFailure, type TransportCallOptions } from './transportTypes'

interface WorkspaceReferenceTransport {
  readonly generation?: number
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: TransportCallOptions): Promise<T>
}

export function createV4WorkspaceReferences(transport: WorkspaceReferenceTransport): WorkspaceReferences {
  return {
    async read(sessionKey, reference, signal) {
      try {
        const params = { sessionKey, reference }
        if (!validateWorkspacesReferencesReadParams(params)) throw new WorkspaceReferenceError('INVALID_REFERENCE')
        const generation = transport.generation
        const raw = await transport.request<Record<string, unknown>>(
          WORKSPACES_REFERENCES_READ_METHOD,
          params,
          { signal, expectedGeneration: generation, timeoutMs: 15_000, timeoutAction: 'reject', abortAction: 'reject' },
        )
        if (signal?.aborted || generation !== transport.generation) throw new WorkspaceReferenceError('UNAVAILABLE')
        const normalized = normalizeWorkspaceFileReferenceV1(raw.reference)
        if (!validateWorkspacesReferencesReadResult(raw) || !normalized
          || normalized.scope.sessionKey !== sessionKey
          || normalized.locator.relativePath !== reference.locator.relativePath
          || raw.relativePath !== normalized.locator.relativePath
          || raw.revision !== normalized.state?.revision
          || raw.startLine !== reference.locator.startLine || raw.endLine !== reference.locator.endLine
          || (reference.scope.workspaceId && normalized.scope.workspaceId !== reference.scope.workspaceId)
          || (reference.state?.revision && raw.revision !== reference.state.revision)) {
          throw new WorkspaceReferenceError('INVALID_REFERENCE')
        }
        return { ...raw, reference: normalized } as unknown as WorkspaceSourceSnapshot
      } catch (error) {
        if (error instanceof WorkspaceReferenceError) throw error
        throw new WorkspaceReferenceError(readTransportFailure(error).code || 'UNAVAILABLE')
      }
    },
  }
}
