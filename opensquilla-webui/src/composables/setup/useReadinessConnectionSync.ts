import { watch } from 'vue'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import { optionalSessionRpcAllowed } from '@/composables/chat/sessionBootstrapAdmission'
import type { TokenRhythmAccountState } from '../../../../contracts/desktop/tokenrhythm-account'
import { invalidateReadiness } from './readinessInvalidation'

interface ReadinessAccount {
  readonly supported: boolean
  readonly ready: boolean
  readonly state: TokenRhythmAccountState | null
}

/** App-scoped refresh for native account changes/restarts outside WebUI Settings. */
export function useReadinessConnectionSync(
  access: Pick<GatewayAccess, 'isAvailable' | 'subscriptionEpoch'>,
  account?: ReadinessAccount,
): void {
  let refreshPending = false
  watch(
    [
      () => access.isAvailable,
      () => access.subscriptionEpoch,
      optionalSessionRpcAllowed,
      // Snapshot revisions also change for wallet/profile polling. Only account
      // fields that affect the shared setup status should invalidate its cache.
      () => account?.supported ? account.ready : undefined,
      () => account?.supported ? account.state?.source : undefined,
      () => account?.supported ? account.state?.selectedAccountId : undefined,
      () => account?.supported ? account.state?.profile?.accountId : undefined,
      () => account?.supported ? account.state?.profile?.sessionId : undefined,
      () => account?.supported ? account.state?.mediaSources.image : undefined,
      () => account?.supported ? account.state?.mediaSources.video : undefined,
    ],
    ([available, epoch, allowed, ...accountState], [wasAvailable, previousEpoch, , ...previousAccountState]) => {
      if (
        (available && (!wasAvailable || epoch !== previousEpoch))
        || accountState.some((value, index) => value !== previousAccountState[index])
      ) refreshPending = true
      if (!available || !allowed || !refreshPending) return
      refreshPending = false
      invalidateReadiness()
    },
  )
}
