import type { RpcCallOptions } from '@/lib/rpc'
import type {
  CapabilitySetup,
  ProfileLifecycle,
  ProviderSetup,
  SetupCatalog,
  SetupDiscoveryResult,
  SetupRequestOptions,
  SetupStatus,
  SetupWorkflow,
} from '@/modules/setupWorkflow'
import { ONBOARDING_CATALOG_METHOD } from '@/contracts/generated/v4/onboardingCatalog'
import { validateResult as validateOnboardingCatalogResult } from '@/contracts/generated/v4/onboardingCatalogValidators.mjs'
import { setupContracts, type SetupContractDescriptor } from './platformSetupContracts'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready?(options?: { timeoutMs?: number; signal?: AbortSignal }): Promise<void>
  supports?(method: string): boolean
}

const options = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 20_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})

const wireParams = (value: object): Record<string, unknown> => ({ ...value })

const object = (result: unknown, method: string): Record<string, unknown> => {
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new Error(`${method} returned an invalid response`)
  }
  return result as Record<string, unknown>
}

async function requestContract(
  rpc: RpcTransport,
  contract: SetupContractDescriptor,
  params: Record<string, unknown> | undefined,
  request?: SetupRequestOptions,
): Promise<Record<string, unknown>> {
  const result = await rpc.request(contract.method, params, options(request?.signal))
  if (!contract.validateResult(result)) {
    throw new Error(`${contract.method} returned an invalid response`)
  }
  return object(result, contract.method)
}

function isMethodUnavailable(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return /method.*not found|unknown method|not registered/i.test(message)
}

export function createV4SetupWorkflow(rpc: RpcTransport): SetupWorkflow {
  const provider: ProviderSetup = {
    configurePrimary(command, request) {
      return requestContract(rpc, setupContracts.providerConfigure, wireParams(command), request)
    },
    probePrimary(command, request) {
      return requestContract(rpc, setupContracts.providerProbe, wireParams(command), request)
    },
    discoverPrimaryModels(command, request) {
      return requestContract(rpc, setupContracts.modelsDiscover, wireParams(command), request) as Promise<SetupDiscoveryResult>
    },
    revealActiveCredential(providerId, request) {
      return requestContract(rpc, setupContracts.credentialReveal, { providerId }, request)
    },
    clearActiveCredential(providerId, request) {
      return requestContract(rpc, setupContracts.credentialClear, { providerId }, request)
    },
  }

  const profile: ProfileLifecycle = {
    upsertProfile(command, request) {
      return requestContract(rpc, setupContracts.profileUpsert, wireParams(command), request)
    },
    activateProfile(command, request) {
      return requestContract(rpc, setupContracts.profileActivate, wireParams(command), request)
    },
    probeProfile(command, request) {
      return requestContract(rpc, setupContracts.profileProbe, wireParams(command), request)
    },
    probeDraftProfile(command, request) {
      return requestContract(rpc, setupContracts.profileDraftProbe, wireParams(command), request) as Promise<SetupDiscoveryResult>
    },
    async discoverProfileModels(command, request) {
      try {
        return await requestContract(rpc, setupContracts.profileModelsDiscover, wireParams(command), request) as SetupDiscoveryResult
      } catch (error) {
        if (!isMethodUnavailable(error)) throw error
        return provider.discoverPrimaryModels(command, request)
      }
    },
    discoverDraftProfileModels(command, request) {
      return requestContract(rpc, setupContracts.profileDraftModelsDiscover, wireParams(command), request) as Promise<SetupDiscoveryResult>
    },
    removeProfile(providerId, request) {
      return requestContract(rpc, setupContracts.profileRemove, { providerId }, request)
    },
    removeActiveProfile(command, request) {
      return requestContract(rpc, setupContracts.profileActiveRemove, wireParams(command), request)
    },
    clearProfileCredential(providerId, request) {
      return requestContract(rpc, setupContracts.profileCredentialClear, { providerId }, request)
    },
  }

  const capability: CapabilitySetup = {
    configureRouter(command, request) {
      return requestContract(rpc, setupContracts.routerConfigure, wireParams(command), request)
    },
    configureEnsemble(command, request) {
      return requestContract(rpc, setupContracts.ensembleConfigure, wireParams(command), request)
    },
    configureSearch(command, request) {
      return requestContract(rpc, setupContracts.searchConfigure, wireParams(command), request)
    },
    configureImageGeneration(command, request) {
      return requestContract(rpc, setupContracts.imageGenerationConfigure, wireParams(command), request)
    },
    configureMemoryEmbedding(command, request) {
      return requestContract(rpc, setupContracts.memoryEmbeddingConfigure, wireParams(command), request)
    },
    configureAudio(command, request) {
      return requestContract(rpc, setupContracts.audioConfigure, wireParams(command), request)
    },
    resetCapability(capabilityId, request) {
      return requestContract(rpc, setupContracts.capabilityReset, { capabilityId }, request)
    },
  }

  return {
    capabilities: {
      get profileLifecycle() {
        return rpc.supports?.(setupContracts.profileUpsert.method) !== false
      },
      get primaryProviderRemoval() {
        return rpc.supports?.(setupContracts.profileActiveRemove.method) !== false
      },
      get imageModelDiscovery() {
        return rpc.supports?.(setupContracts.imageModelsDiscover.method) !== false
      },
    },
    async catalog(request) {
      const result = await rpc.request(ONBOARDING_CATALOG_METHOD, undefined, options(request?.signal))
      if (!validateOnboardingCatalogResult(result)) throw new Error(`${ONBOARDING_CATALOG_METHOD} returned an invalid response`)
      return result as SetupCatalog
    },
    async status(request) {
      await rpc.ready?.({ timeoutMs: 20_000, signal: request?.signal })
      return requestContract(rpc, setupContracts.status, undefined, request) as Promise<SetupStatus>
    },
    discoverImageGenerationModels(providerId, request) {
      return requestContract(rpc, setupContracts.imageModelsDiscover, { providerId }, request) as Promise<SetupDiscoveryResult>
    },
    provider,
    profile,
    capability,
  }
}
