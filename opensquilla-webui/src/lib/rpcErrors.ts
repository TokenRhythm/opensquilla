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

/** "Save failed: <localized>" — the common onboarding save-toast string. */
export function saveFailedMessage(err: unknown): string {
  return `${i18n.global.t('errors.saveFailed')}: ${localizeRpcError(err)}`
}
