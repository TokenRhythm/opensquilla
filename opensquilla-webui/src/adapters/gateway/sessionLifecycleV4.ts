import type { RpcCallOptions } from '@/lib/rpc'
import {
  SESSIONS_CREATE_METHOD,
  type SessionsCreateParams,
  type SessionsCreateResult,
} from '@/contracts/generated/v4/sessionsCreate'
import { validateSessionsCreateResult } from '@/contracts/generated/v4/sessionsCreateValidators.mjs'
import {
  SESSIONS_RENAME_METHOD,
  type SessionsRenameParams,
  type SessionsRenameResult,
} from '@/contracts/generated/v4/sessionsRename'
import { validateSessionsRenameResult } from '@/contracts/generated/v4/sessionsRenameValidators.mjs'
import {
  SESSIONS_DELETE_METHOD,
  type SessionsDeleteParams,
  type SessionsDeleteResult,
} from '@/contracts/generated/v4/sessionsDelete'
import { validateSessionsDeleteResult } from '@/contracts/generated/v4/sessionsDeleteValidators.mjs'
import type {
  CreatedSession,
  SessionCreateRequest,
  SessionDeleteResult as DomainDeleteResult,
  SessionLifecycle,
  SessionRenameRequest,
  SessionRenameResult as DomainRenameResult,
} from '@/modules/sessionLifecycle'
import { SessionLifecycleError } from '@/modules/sessionLifecycle'

/**
 * Narrow wire port owned by this Adapter.  The generic transport itself is
 * deliberately not part of the SessionLifecycle interface or its callers.
 * Mutations retain RpcClient's existing call semantics: no implicit ready or
 * reconnect is introduced by this migration.
 */
interface SessionLifecycleTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}

function optionsFor(signal: AbortSignal | undefined): RpcCallOptions | undefined {
  return signal ? { signal } : undefined
}

function wireErrorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const candidate = error as { code?: unknown; data?: { code?: unknown } }
  const code = candidate.code ?? candidate.data?.code
  return typeof code === 'string' ? code.toUpperCase() : ''
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true || (error instanceof Error && error.name === 'AbortError')
}

function lifecycleError(error: unknown): SessionLifecycleError {
  if (error instanceof SessionLifecycleError) return error
  const code = wireErrorCode(error)
  const message = error instanceof Error ? error.message : 'Session lifecycle request failed'
  if (
    code === 'NOT_FOUND'
    || code === 'SESSION_NOT_FOUND'
    || code === 'AGENT.NOT_FOUND'
    || code === 'AGENT_NOT_FOUND'
    || code === 'WORKSPACE_NOT_FOUND'
  ) {
    return new SessionLifecycleError('not-found', message, { cause: error })
  }
  if (code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED') {
    return new SessionLifecycleError('unsupported', message, { cause: error })
  }
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN' || code === 'OWNER_REQUIRED') {
    return new SessionLifecycleError('forbidden', message, { cause: error })
  }
  if (code === 'CONFLICT') {
    return new SessionLifecycleError('conflict', message, { cause: error })
  }
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS') {
    return new SessionLifecycleError('invalid', message, { cause: error })
  }
  return new SessionLifecycleError('unavailable', message, { cause: error })
}

function responseError(message: string): SessionLifecycleError {
  return new SessionLifecycleError('unavailable', message)
}

function createParams(request: SessionCreateRequest | undefined): SessionsCreateParams {
  const params: SessionsCreateParams = {}
  if (!request) return params
  if (request.agentId !== undefined) params.agentId = request.agentId
  if (request.kind !== undefined) params.kind = request.kind
  if (request.workspaceId !== undefined) params.workspaceId = request.workspaceId
  // `title` is the domain term; displayName is a v4 wire alias hidden here.
  if (request.title !== undefined) params.displayName = request.title
  if (request.message !== undefined) params.message = request.message
  if (request.model !== undefined) params.model = request.model
  return params
}

export function createV4SessionLifecycle(
  transport: SessionLifecycleTransport,
): SessionLifecycle {
  return {
    async create(request: SessionCreateRequest = {}): Promise<CreatedSession> {
      try {
        const raw = await transport.request<SessionsCreateResult>(
          SESSIONS_CREATE_METHOD,
          createParams(request),
          optionsFor(request.signal),
        )
        if (!validateSessionsCreateResult(raw)) {
          throw responseError('sessions.create returned an invalid response')
        }
        return {
          key: raw.key,
          sessionId: raw.sessionId,
          ...(raw.seededMessage !== undefined ? { seededMessage: raw.seededMessage } : {}),
          ...(raw.note !== undefined ? { note: raw.note } : {}),
        }
      } catch (error) {
        if (isAbort(error, request.signal)) throw error
        throw lifecycleError(error)
      }
    },

    async rename(request: SessionRenameRequest): Promise<DomainRenameResult> {
      try {
        const params: SessionsRenameParams = {
          key: request.key,
          displayName: request.title,
        }
        const raw = await transport.request<SessionsRenameResult>(
          SESSIONS_RENAME_METHOD,
          params,
          optionsFor(request.signal),
        )
        if (!validateSessionsRenameResult(raw)) {
          throw responseError('sessions.rename returned an invalid response')
        }
        return { key: raw.key, updatedFields: [...raw.updated] }
      } catch (error) {
        if (isAbort(error, request.signal)) throw error
        throw lifecycleError(error)
      }
    },

    async remove(
      keys: readonly string[],
      options: { signal?: AbortSignal } = {},
    ): Promise<DomainDeleteResult> {
      try {
        const params: SessionsDeleteParams = { keys: [...keys] }
        const raw = await transport.request<SessionsDeleteResult>(
          SESSIONS_DELETE_METHOD,
          params,
          optionsFor(options.signal),
        )
        if (!validateSessionsDeleteResult(raw)) {
          throw responseError('sessions.delete returned an invalid response')
        }
        return {
          deleted: [...raw.deleted],
          // The v4 implementation emits strings.  Keep the domain result
          // narrow while preserving every partial-failure entry verbatim.
          errors: [...raw.errors],
        }
      } catch (error) {
        if (isAbort(error, options.signal)) throw error
        throw lifecycleError(error)
      }
    },
  }
}
