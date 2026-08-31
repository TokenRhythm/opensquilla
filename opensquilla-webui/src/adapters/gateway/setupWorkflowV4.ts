import type { RpcCallOptions } from '@/lib/rpc'
import type { SetupCatalog, SetupProfile, SetupStatus, SetupWorkflow } from '@/modules/setupWorkflow'
import { ONBOARDING_CATALOG_METHOD } from '@/contracts/generated/v4/onboardingCatalog'
import { validateResult as validateOnboardingCatalogResult } from '@/contracts/generated/v4/onboardingCatalogValidators.mjs'

interface RpcTransport { request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T> }
const options = (signal?: AbortSignal): RpcCallOptions => ({ timeoutMs: 20_000, timeoutAction: 'reject', abortAction: 'reject', ...(signal ? { signal } : {}) })
const value = <T>(result: unknown): T => result as T

export function createV4SetupWorkflow(rpc: RpcTransport): SetupWorkflow {
  return {
    async catalog(request) {
      const result = await rpc.request(ONBOARDING_CATALOG_METHOD, undefined, options(request?.signal))
      if (!validateOnboardingCatalogResult(result)) throw new Error(`${ONBOARDING_CATALOG_METHOD} returned an invalid response`)
      return value<SetupCatalog>(result)
    },
    async status(request) { return value<SetupStatus>(await rpc.request('onboarding.status', undefined, options(request?.signal))) },
    async configure(profile: SetupProfile, request) { return value<SetupStatus>(await rpc.request('onboarding.configure', profile, options(request?.signal))) },
    async reset(request) { return value<SetupStatus>(await rpc.request('onboarding.reset', undefined, options(request?.signal))) },
  }
}
