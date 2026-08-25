import i18n from '@/i18n'
import type { RpcClientError } from '@/lib/rpc'

// Stable backend error codes (raised by gateway/rpc_onboarding.py) mapped to
// i18n message keys. A code not listed here falls back to the raw English
// err.message, so adding a backend code without a key degrades gracefully
// rather than rendering blank.
const RPC_ERROR_KEYS: Record<string, string> = {
  'onboarding.provider.invalid': 'errors.onboarding.provider',
  'onboarding.router.invalid': 'errors.onboarding.router',
  'onboarding.search.invalid': 'errors.onboarding.search',
  'onboarding.imageGeneration.invalid': 'errors.onboarding.image',
  'onboarding.channel.invalid': 'errors.onboarding.channel',
  'onboarding.channel.not_found': 'errors.onboarding.channelNotFound',
}

// Goal mutations have a stable domain taxonomy. Unlike onboarding errors,
// these strings are commonly action-oriented status conflicts, so exposing the
// raw backend detail (for example, "The Goal still owns an unsettled task")
// makes the localized control surface feel broken and can describe a state the
// freshly returned snapshot has already superseded.
const GOAL_RPC_ERROR_KEYS: Record<string, string> = {
  INVALID_GOAL_OBJECTIVE: 'chat.goal.errors.invalidObjective',
  INVALID_GOAL_COMMAND: 'chat.goal.errors.invalidCommand',
  INVALID_GOAL_PROGRESS: 'chat.goal.errors.invalidCommand',
  INVALID_GOAL_REASON: 'chat.goal.errors.invalidCommand',
  INVALID_GOAL_GUARDRAIL: 'chat.goal.errors.invalidCommand',
  GOAL_NOT_FOUND: 'chat.goal.errors.notFound',
  SESSION_GENERATION_CHANGED: 'chat.goal.errors.sessionChanged',
  STALE_GOAL: 'chat.goal.errors.changed',
  GOAL_ACTIVE: 'chat.goal.errors.alreadyActive',
  GOAL_BUSY: 'chat.goal.errors.busy',
  GOAL_NOT_RESUMABLE: 'chat.goal.errors.notResumable',
  GOAL_EXECUTION_DISABLED: 'chat.goal.errors.executionDisabled',
  EXECUTION_LEASE_REQUIRED: 'chat.goal.errors.connectionRequired',
  PLAN_MODE_ACTIVE: 'chat.goal.errors.planModeActive',
  PLAN_RUN_ACTIVE: 'chat.goal.errors.planRunActive',
  IDEMPOTENCY_CONFLICT: 'chat.goal.errors.requestConflict',
}

/**
 * Localized message for an RPC error: a translated lead for known stable codes
 * (with the original English detail appended in parentheses so the specifics are
 * never lost), otherwise the raw error message.
 */
export function localizeRpcError(err: unknown): string {
  const t = i18n.global.t
  const code = (err as RpcClientError | undefined)?.code
  const detail = err instanceof Error ? err.message : String(err ?? '')
  if (code && code in RPC_ERROR_KEYS) {
    const lead = t(RPC_ERROR_KEYS[code])
    return detail ? `${lead} (${detail})` : lead
  }
  return detail
}

/** Localize stable Goal conflicts without leaking backend-only English copy. */
export function localizeGoalRpcError(err: unknown): string {
  const code = (err as RpcClientError | undefined)?.code
  if (code && code in GOAL_RPC_ERROR_KEYS) {
    return i18n.global.t(GOAL_RPC_ERROR_KEYS[code])
  }
  return localizeRpcError(err)
}

/** "Save failed: <localized>" — the common onboarding save-toast string. */
export function saveFailedMessage(err: unknown): string {
  return `${i18n.global.t('errors.saveFailed')}: ${localizeRpcError(err)}`
}
