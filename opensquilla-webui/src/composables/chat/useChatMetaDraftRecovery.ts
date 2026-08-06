import type { DurableMetaDraft } from '@/composables/chat/useChatSlashCommands'
import {
  isAuthoritativeSessionSubscription,
  type SessionSubscriptionResult,
} from '@/composables/chat/useChatSessionSubscription'
import type { MetaDraftsListResponse } from '@/types/rpc'

const META_DRAFTS_LIST_METHOD = 'meta.drafts.list'

export type MetaDraftQuery = { sessionKey?: string, agentId?: string }

export interface MetaDraftListRpc {
  waitForConnection: (timeoutMs?: number) => Promise<void>
  supportsMethod: (method: string) => boolean
  markMethodUnavailable: (method: string) => void
  call: (method: string, params?: Record<string, unknown>) => Promise<unknown>
}

export interface MetaDraftListResult {
  drafts: DurableMetaDraft[]
  retryable: boolean
}

function isMethodNotFound(error: unknown): boolean {
  const candidate = error as { code?: unknown, message?: unknown } | null
  return candidate?.code === 'METHOD_NOT_FOUND'
    || /method not found/i.test(error instanceof Error ? error.message : String(candidate?.message || error))
}

export async function queryServerMetaDrafts(
  rpc: MetaDraftListRpc,
  query: MetaDraftQuery,
): Promise<MetaDraftListResult> {
  try {
    // waitForConnection resolves after hello has populated the advertised method
    // set, so mixed-version gateways never receive an unsupported request.
    await rpc.waitForConnection(15_000)
    if (!rpc.supportsMethod(META_DRAFTS_LIST_METHOD)) {
      return { drafts: [], retryable: false }
    }
    const result = await rpc.call(META_DRAFTS_LIST_METHOD, query) as MetaDraftsListResponse
    return {
      drafts: Array.isArray(result?.drafts) ? result.drafts : [],
      retryable: false,
    }
  } catch (error) {
    const unavailable = isMethodNotFound(error)
    if (unavailable) rpc.markMethodUnavailable(META_DRAFTS_LIST_METHOD)
    // Browser outboxes remain the compatibility fallback for older or
    // temporarily unavailable gateways.
    return { drafts: [], retryable: !unavailable }
  }
}

export async function listServerMetaDrafts(
  rpc: MetaDraftListRpc,
  query: MetaDraftQuery,
): Promise<DurableMetaDraft[]> {
  return (await queryServerMetaDrafts(rpc, query)).drafts
}

export interface ChatMetaDraftRecoveryOptions {
  currentSessionKey: () => string
  listDrafts: (query: MetaDraftQuery) => Promise<MetaDraftListResult>
  isPristineDraft: (sessionKey: string, agentId: string) => boolean
  rebindDraftSession: (
    sessionKey: string,
    guard: (sourceSessionKey: string) => boolean,
  ) => Promise<SessionSubscriptionResult>
  onAuthoritativeSubscription: (
    sessionKey: string,
    prefetchedDrafts: DurableMetaDraft[],
  ) => void | Promise<void>
}

/**
 * Runs provisional Meta draft discovery outside the normal chat bootstrap.
 * Every async boundary is generation-guarded so late results cannot take over
 * a draft after typing, sending, navigation, or unmount.
 */
export function createChatMetaDraftRecovery(options: ChatMetaDraftRecoveryOptions) {
  let generation = 0
  let startedIdentity = ''
  let retryIdentity = ''

  function invalidate(): void {
    generation += 1
    startedIdentity = ''
    retryIdentity = ''
  }

  function start(agentId: string): void {
    const sourceSessionKey = options.currentSessionKey()
    if (!sourceSessionKey || !options.isPristineDraft(sourceSessionKey, agentId)) return
    const identity = `${sourceSessionKey}\u0000${agentId}`
    if (identity === startedIdentity) return
    startedIdentity = identity
    retryIdentity = ''
    const attempt = ++generation

    void recover(attempt, sourceSessionKey, agentId)
      .then((retryable) => {
        if (retryable && attempt === generation && startedIdentity === identity) {
          retryIdentity = identity
        }
      })
      .catch((error: unknown) => {
        if (attempt === generation && startedIdentity === identity) retryIdentity = identity
        console.warn(
          'Provisional Meta draft recovery failed:',
          error instanceof Error ? error.message : error,
        )
      })
  }

  function retry(agentId: string): void {
    const sourceSessionKey = options.currentSessionKey()
    const identity = `${sourceSessionKey}\u0000${agentId}`
    if (identity !== retryIdentity) return
    startedIdentity = ''
    start(agentId)
  }

  async function recover(
    attempt: number,
    sourceSessionKey: string,
    agentId: string,
  ): Promise<boolean> {
    const result = await options.listDrafts({ agentId })
    if (result.retryable) return true
    const drafts = result.drafts
    if (!canAdopt(attempt, sourceSessionKey, agentId)) return false

    const provisional = drafts.find(draft => draft.sessionExists === false)
    if (!provisional || provisional.sessionKey === sourceSessionKey) return false

    const outcome = await options.rebindDraftSession(
      provisional.sessionKey,
      currentSourceSessionKey => (
        currentSourceSessionKey === sourceSessionKey
        && canAdopt(attempt, sourceSessionKey, agentId)
      ),
    )
    if (
      attempt !== generation
      || options.currentSessionKey() !== provisional.sessionKey
      || !options.isPristineDraft(provisional.sessionKey, agentId)
      || !isAuthoritativeSessionSubscription(outcome)
    ) {
      return false
    }

    await options.onAuthoritativeSubscription(
      provisional.sessionKey,
      drafts.filter(draft => draft.sessionKey === provisional.sessionKey),
    )
    return false
  }

  function canAdopt(attempt: number, sourceSessionKey: string, agentId: string): boolean {
    return attempt === generation
      && options.currentSessionKey() === sourceSessionKey
      && options.isPristineDraft(sourceSessionKey, agentId)
  }

  return { invalidate, retry, start }
}
