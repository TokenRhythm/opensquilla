import type { Params as GitDiffContractParams, Result as GitDiffContractResult } from '@/contracts/generated/v4/gitDiff'
import type { Params as GitStatusContractParams, Result as GitStatusContractResult } from '@/contracts/generated/v4/gitStatus'
import { validateParams as validateGitDiffParams, validateResult as validateGitDiffResult } from '@/contracts/generated/v4/gitDiffValidators.mjs'
import { validateParams as validateGitStatusParams, validateResult as validateGitStatusResult } from '@/contracts/generated/v4/gitStatusValidators.mjs'
import type { GitAccess, GitDiffPayload, GitStatusPayload } from '@/modules/git'
import type { TransportCallOptions } from './transportTypes'

interface RpcTransport {
  request<T = unknown>(method: string, params?: object, options?: TransportCallOptions): Promise<T>
}

const METHODS = {
  status: 'git.status',
  diff: 'git.diff',
} as const

const requestOptions = (signal?: AbortSignal): TransportCallOptions => ({
  timeoutMs: 15_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  recoveryClass: 'ephemeral',
  ...(signal ? { signal } : {}),
})

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

/**
 * The generated Contract validator owns field presence, nullability, enums and
 * integer bounds, so a malformed payload never reaches the panel. Returning the
 * Contract projection under the module type keeps both shapes in lockstep.
 */
function parseStatus(payload: unknown): GitStatusPayload {
  if (!validateGitStatusResult(payload)) throw invalid(METHODS.status)
  return (payload as GitStatusContractResult).status
}

function parseDiff(payload: unknown): GitDiffPayload {
  if (!validateGitDiffResult(payload)) throw invalid(METHODS.diff)
  return (payload as GitDiffContractResult).diff
}

export function createGitAccess(transport: RpcTransport): GitAccess {
  return {
    async status(options = {}) {
      const params: GitStatusContractParams = { workspaceId: options.workspaceId ?? null }
      if (!validateGitStatusParams(params)) throw new Error('git.status params violated Contract')
      return parseStatus(await transport.request(METHODS.status, params, requestOptions(options.signal)))
    },
    async diff(input) {
      if (!input || typeof input.path !== 'string' || !input.path) {
        throw new Error('invalid git.diff input')
      }
      const params: GitDiffContractParams = {
        path: input.path,
        workspaceId: input.workspaceId ?? null,
      }
      if (input.staged !== undefined) params.staged = input.staged
      if (!validateGitDiffParams(params)) throw new Error('git.diff params violated Contract')
      return parseDiff(await transport.request(METHODS.diff, params, requestOptions(input.signal)))
    },
  }
}
