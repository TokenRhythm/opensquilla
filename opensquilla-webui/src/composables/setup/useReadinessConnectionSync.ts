import { watch } from 'vue'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import { optionalSessionRpcAllowed } from '@/composables/chat/sessionBootstrapAdmission'
import { invalidateReadiness } from './readinessInvalidation'

/** App-scoped refresh for native saves/restarts that bypass WebUI Settings. */
export function useReadinessConnectionSync(
  access: Pick<GatewayAccess, 'isAvailable' | 'subscriptionEpoch'>,
): void {
  let refreshPending = false
  watch(
    [() => access.isAvailable, () => access.subscriptionEpoch, optionalSessionRpcAllowed],
    ([available, epoch, allowed], [wasAvailable, previousEpoch]) => {
      if (available && (!wasAvailable || epoch !== previousEpoch)) refreshPending = true
      if (!available || !allowed || !refreshPending) return
      refreshPending = false
      invalidateReadiness()
    },
  )
}
