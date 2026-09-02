import type { RpcCallOptions } from '@/lib/rpc'
import type {
  CapabilitySetup,
  ProfileLifecycle,
  ProviderSetup,
  SetupCatalog,
  SetupDiscoveryResult,
  SetupPayload,
  SetupStatus,
  SetupWorkflow,
} from '@/modules/setupWorkflow'
import { ONBOARDING_CATALOG_METHOD } from '@/contracts/generated/v4/onboardingCatalog'
import { validateResult as validateOnboardingCatalogResult } from '@/contracts/generated/v4/onboardingCatalogValidators.mjs'

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
const object = (result: unknown, method: string): Record<string, unknown> => {
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new Error(`${method} returned an invalid response`)
  }
  return result as Record<string, unknown>
}

const discovery = (result: unknown, method: string): SetupDiscoveryResult => (
  object(result, method) as SetupDiscoveryResult
)

function isMethodUnavailable(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)
  return /method.*not found|unknown method|not registered/i.test(message)
}

const capabilityMethods: Record<string, string> = {
  router: 'onboarding.router.configure',
  ensemble: 'onboarding.ensemble.configure',
  search: 'onboarding.search.configure',
  imageGeneration: 'onboarding.imageGeneration.configure',
  memory_embedding: 'onboarding.memory_embedding.configure',
  audio: 'onboarding.audio.configure',
}

const capabilityIds: Record<string, string> = {
  router: 'router',
  ensemble: 'ensemble',
  search: 'search',
  imageGeneration: 'image_generation',
  image_generation: 'image_generation',
  memoryEmbedding: 'memory_embedding',
  memory_embedding: 'memory_embedding',
  audio: 'audio',
}

export function createV4SetupWorkflow(rpc: RpcTransport): SetupWorkflow {
  const provider: ProviderSetup = {
    async configure(payload, request) {
      return object(await rpc.request('onboarding.provider.configure', payload, options(request?.signal)), 'onboarding.provider.configure')
    },
    async probe(payload, request) {
      return object(await rpc.request('onboarding.provider.probe', payload, options(request?.signal)), 'onboarding.provider.probe')
    },
    async discoverModels(payload, request) {
      return discovery(await rpc.request('onboarding.models.discover', payload, options(request?.signal)), 'onboarding.models.discover')
    },
    async credentialReveal(providerId, request) {
      return object(await rpc.request('onboarding.provider.credential.reveal', { providerId }, options(request?.signal)), 'onboarding.provider.credential.reveal')
    },
    async credentialClear(providerId, request) {
      return object(await rpc.request('onboarding.provider.credential.clear', { providerId }, options(request?.signal)), 'onboarding.provider.credential.clear')
    },
  }
  const profile: ProfileLifecycle = {
    async upsert(payload, request) {
      return object(await rpc.request('onboarding.llmProfile.upsert', payload, options(request?.signal)), 'onboarding.llmProfile.upsert')
    },
    async activate(payload, request) {
      return object(await rpc.request('onboarding.llmProfile.activate', payload, options(request?.signal)), 'onboarding.llmProfile.activate')
    },
    async probe(payload, request) {
      return object(await rpc.request('onboarding.llmProfile.probe', payload, options(request?.signal)), 'onboarding.llmProfile.probe')
    },
    async probeDraft(payload, request) {
      return discovery(await rpc.request('onboarding.llmProfile.draft.probe', payload, options(request?.signal)), 'onboarding.llmProfile.draft.probe')
    },
    async discoverModels(payload, request) {
      try {
        return discovery(
          await rpc.request('onboarding.llmProfile.models.discover', payload, options(request?.signal)),
          'onboarding.llmProfile.models.discover',
        )
      } catch (error) {
        if (!isMethodUnavailable(error)) throw error
        return provider.discoverModels(payload, request)
      }
    },
    async discoverDraftModels(payload, request) {
      return discovery(await rpc.request('onboarding.llmProfile.draft.models.discover', payload, options(request?.signal)), 'onboarding.llmProfile.draft.models.discover')
    },
    async remove(providerId, request) {
      return object(await rpc.request('onboarding.llmProfile.remove', { providerId }, options(request?.signal)), 'onboarding.llmProfile.remove')
    },
    async removeActive(payload, request) {
      return object(await rpc.request('onboarding.llmProfile.active.remove', payload, options(request?.signal)), 'onboarding.llmProfile.active.remove')
    },
    async credentialClear(providerId, request) {
      return object(await rpc.request('onboarding.llmProfile.credential.clear', { providerId }, options(request?.signal)), 'onboarding.llmProfile.credential.clear')
    },
  }
  const capability: CapabilitySetup = {
    async configure(name: string, payload: SetupPayload, request) {
      const method = capabilityMethods[name]
      if (!method) throw new Error(`Unsupported setup capability: ${name}`)
      return object(await rpc.request(method, payload, options(request?.signal)), method)
    },
    async reset(name: string, request) {
      const capabilityId = capabilityIds[name]
      if (!capabilityId) {
        throw new Error(`Unsupported setup capability: ${name}`)
      }
      return object(await rpc.request('onboarding.capability.reset', { capabilityId }, options(request?.signal)), 'onboarding.capability.reset')
    },
  }

  return {
    capabilities: {
      get profileLifecycle() {
        return rpc.supports?.('onboarding.llmProfile.upsert') !== false
      },
      get primaryProviderRemoval() {
        return rpc.supports?.('onboarding.llmProfile.active.remove') !== false
      },
      get imageModelDiscovery() {
        return rpc.supports?.('onboarding.imageGeneration.models.discover') !== false
      },
    },
    async catalog(request) {
      const result = await rpc.request(ONBOARDING_CATALOG_METHOD, undefined, options(request?.signal))
      if (!validateOnboardingCatalogResult(result)) throw new Error(`${ONBOARDING_CATALOG_METHOD} returned an invalid response`)
      return result as SetupCatalog
    },
    async status(request) {
      await rpc.ready?.({ timeoutMs: 20_000, signal: request?.signal })
      return object(await rpc.request('onboarding.status', undefined, options(request?.signal)), 'onboarding.status') as SetupStatus
    },
    async discoverImageGenerationModels(providerId, request) {
      return discovery(
        await rpc.request('onboarding.imageGeneration.models.discover', { providerId }, options(request?.signal)),
        'onboarding.imageGeneration.models.discover',
      )
    },
    provider,
    profile,
    capability,
  }
}
