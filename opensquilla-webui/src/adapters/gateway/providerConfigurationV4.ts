import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import { readTransportFailure } from './privateTransports'
import type {
  ModelCatalogResult,
  ModelDescriptor,
  ModelRoutingSnapshot,
  ProviderConfiguration,
  ProviderDescriptor,
  ProviderListError,
  ProviderStatusResult,
  ProviderStatusRow,
  ProviderLatency,
  ProviderModelProbe,
  RoutingMode,
} from '@/modules/providerConfiguration'
import { ProviderConfigurationError } from '@/modules/providerConfiguration'
import { MODELS_ROUTING_GET_METHOD } from '@/contracts/generated/v4/modelsRoutingGet'
import { validateResult as validateModelsRoutingGetResult } from '@/contracts/generated/v4/modelsRoutingGetValidators.mjs'
import { MODELS_LIST_METHOD } from '@/contracts/generated/v4/modelsList'
import { validateResult as validateModelsListResult } from '@/contracts/generated/v4/modelsListValidators.mjs'
import { MODELS_ROUTING_SET_METHOD } from '@/contracts/generated/v4/modelsRoutingSet'
import { validateParams as validateModelsRoutingSetParams, validateResult as validateModelsRoutingSetResult } from '@/contracts/generated/v4/modelsRoutingSetValidators.mjs'
import { MODELS_ROUTING_CHANGED_EVENT } from '@/contracts/generated/v4/modelsRoutingChangedEvent'
import { validateModelsRoutingChangedPayload } from '@/contracts/generated/v4/modelsRoutingChangedEventValidators.mjs'
import { ONBOARDING_CATALOG_METHOD } from '@/contracts/generated/v4/onboardingCatalog'
import { validateResult as validateOnboardingCatalogResult } from '@/contracts/generated/v4/onboardingCatalogValidators.mjs'
import { PROVIDERS_STATUS_METHOD } from '@/contracts/generated/v4/providersStatus'
import { validateParams as validateProvidersStatusParams, validateResult as validateProvidersStatusResult } from '@/contracts/generated/v4/providersStatusValidators.mjs'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
}
interface EventTransport {
  subscribe(event: string, handler: (payload: unknown) => void): { close(): void }
}
const options = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 15_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})
const record = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
)

function mapProviderError(error: unknown): ProviderConfigurationError {
  if (error instanceof ProviderConfigurationError) return error
  const failure = readTransportFailure(error)
  const code = failure.code
  const domainCode = code === 'METHOD_NOT_FOUND'
    ? 'unsupported'
    : code === 'NOT_FOUND'
      ? 'not-found'
      : code === 'UNAUTHORIZED' || code === 'FORBIDDEN'
        ? 'forbidden'
        : code?.includes('CONFLICT')
          ? 'conflict'
          : code?.startsWith('INVALID_')
            ? 'invalid'
            : 'unavailable'
  return new ProviderConfigurationError(domainCode, failure.message, error)
}

async function requestProvider<T>(
  rpc: RpcTransport,
  method: string,
  params: Record<string, unknown> | undefined,
  requestOptions: RpcCallOptions,
): Promise<T> {
  try {
    return await rpc.request<T>(method, params, requestOptions)
  } catch (error) {
    throw mapProviderError(error)
  }
}

function providerCatalog(value: unknown): ProviderDescriptor[] {
  const raw = record(value)
  const list = Array.isArray(raw.providers) ? raw.providers : []
  return list
    .filter(item => item && typeof item === 'object' && !Array.isArray(item))
    .map(item => {
      const source = item as Record<string, unknown>
      return {
        ...source,
        providerId: String(source.providerId ?? source.provider_id ?? source.id ?? ''),
      }
    })
    .filter(item => item.providerId)
}

function modelCatalog(value: unknown): ModelCatalogResult {
  const raw = record(value)
  const models: ModelDescriptor[] = Array.isArray(raw.models)
    ? raw.models.filter(item => item && typeof item === 'object' && !Array.isArray(item)).map(item => {
        const source = item as Record<string, unknown>
        const pricing = record(source.pricing)
        return {
          ...source,
          id: String(source.id ?? ''),
          name: String(source.name ?? source.id ?? ''),
          provider: String(source.provider ?? ''),
          contextWindow: Number(source.contextWindow ?? 0),
          maxOutputTokens: Number(source.maxOutputTokens ?? 0),
          capabilities: Array.isArray(source.capabilities)
            ? source.capabilities.filter(item => typeof item === 'string') as string[]
            : [],
          pricing: {
            inputPer1k: Number(pricing.inputPer1k ?? 0),
            outputPer1k: Number(pricing.outputPer1k ?? 0),
          },
          source: String(source.source ?? 'unknown'),
          reasoningFormat: String(source.reasoningFormat ?? 'none'),
          metadata: source.metadata && typeof source.metadata === 'object' && !Array.isArray(source.metadata)
            ? source.metadata as Record<string, unknown>
            : null,
        }
      })
    : []
  const errors: ProviderListError[] = Array.isArray(raw.errors)
    ? raw.errors.filter(item => item && typeof item === 'object' && !Array.isArray(item)).map(item => {
        const source = item as Record<string, unknown>
        return {
          provider: String(source.provider ?? ''),
          kind: String(source.kind ?? 'unknown'),
          detail: String(source.detail ?? ''),
        }
      })
    : []
  return { models, errors }
}

function status(value: unknown): ProviderStatusResult {
  const raw = record(value)
  const rows: ProviderStatusRow[] = Array.isArray(raw.providers)
    ? raw.providers.filter(item => item && typeof item === 'object' && !Array.isArray(item)).map(item => {
        const source = item as Record<string, unknown>
        return {
          ...source,
          providerId: String(source.providerId ?? source.provider_id ?? ''),
          active: source.active === true,
          configured: source.configured === true,
          buildable: source.buildable !== false,
          model: typeof source.model === 'string' ? source.model : null,
          requiresApiKey: source.requiresApiKey === true,
          apiKeyEnv: typeof source.apiKeyEnv === 'string' ? source.apiKeyEnv : null,
          apiKeyConfigured: source.apiKeyConfigured === true,
          apiKeyShape: typeof source.apiKeyShape === 'string' ? source.apiKeyShape : null,
          baseUrlConfigured: source.baseUrlConfigured === true,
          error: typeof source.error === 'string' ? source.error : null,
          modelProbe: modelProbe(source.modelProbe),
          latency: latency(source.latency),
        }
      }).filter(item => item.providerId)
    : []
  return {
    ...raw,
    activeProvider: typeof raw.activeProvider === 'string' ? raw.activeProvider : null,
    providerResolution: record(raw.providerResolution),
    providers: rows,
    count: Number.isInteger(raw.count) ? raw.count as number : rows.length,
  }
}

function latency(value: unknown): ProviderLatency | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  const numeric = (key: string): number | null | undefined => {
    const candidate = source[key]
    return typeof candidate === 'number' && Number.isFinite(candidate) ? candidate : null
  }
  return {
    p50TtftMs: numeric('p50TtftMs'),
    p95TtftMs: numeric('p95TtftMs'),
    samples: numeric('samples'),
    windowMinutes: numeric('windowMinutes'),
  }
}

function modelProbe(value: unknown): ProviderModelProbe | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  return {
    attempted: source.attempted === true,
    status: typeof source.status === 'string' ? source.status : 'unknown',
    count: Number.isInteger(source.count) && Number(source.count) >= 0 ? Number(source.count) : 0,
    error: typeof source.error === 'string' ? source.error : null,
    failureKind: typeof source.failureKind === 'string' ? source.failureKind : null,
  }
}

function routing(value: unknown): ModelRoutingSnapshot {
  const source = record(value)
  const nested = record(source.routing ?? source.modelRouting ?? source.model_routing)
  const mode = String(nested.mode ?? source.mode ?? 'direct')
  return {
    ...nested,
    ...source,
    mode: mode === 'router' || mode === 'ensemble' ? mode : 'direct',
  }
}

export function createV4ProviderConfiguration(
  rpc: RpcTransport,
  events: EventTransport,
): ProviderConfiguration {
  return {
    async catalog(request) {
      const result = await requestProvider(rpc, ONBOARDING_CATALOG_METHOD, undefined, options(request?.signal))
      if (!validateOnboardingCatalogResult(result)) throw new Error(`${ONBOARDING_CATALOG_METHOD} returned an invalid response`)
      return providerCatalog(result)
    },
    async list(request) {
      const result = await requestProvider(rpc, MODELS_LIST_METHOD, undefined, options(request?.signal))
      if (!validateModelsListResult(result)) throw new Error(`${MODELS_LIST_METHOD} returned an invalid response`)
      return modelCatalog(result)
    },
    async status(request) {
      const provider = request?.provider?.trim()
      const params = {
        ...(provider ? { provider } : {}),
        ...(request?.probeModels !== undefined ? { probeModels: request.probeModels } : {}),
      }
      if (!validateProvidersStatusParams(params)) throw new Error(`${PROVIDERS_STATUS_METHOD} params are invalid`)
      const result = await requestProvider(rpc, PROVIDERS_STATUS_METHOD, params, options(request?.signal))
      if (!validateProvidersStatusResult(result)) throw new Error(`${PROVIDERS_STATUS_METHOD} returned an invalid response`)
      return status(result)
    },
    async get(request) {
      const result = await requestProvider(rpc, MODELS_ROUTING_GET_METHOD, undefined, options(request?.signal))
      if (!validateModelsRoutingGetResult(result)) throw new Error(`${MODELS_ROUTING_GET_METHOD} returned an invalid response`)
      return routing(result)
    },
    async setRouting(mode: RoutingMode, request) {
      if (mode !== 'direct' && mode !== 'router' && mode !== 'ensemble') {
        throw new Error(`Unsupported routing mode: ${String(mode)}`)
      }
      const params = { mode }
      if (!validateModelsRoutingSetParams(params)) throw new Error(`${MODELS_ROUTING_SET_METHOD} params are invalid`)
      const result = await requestProvider(rpc, MODELS_ROUTING_SET_METHOD, params, options(request?.signal))
      if (!validateModelsRoutingSetResult(result)) throw new Error(`${MODELS_ROUTING_SET_METHOD} returned an invalid response`)
      return routing(result)
    },
    subscribeChanged(listener) {
      return events.subscribe(MODELS_ROUTING_CHANGED_EVENT, payload => {
        if (!validateModelsRoutingChangedPayload(payload)) return
        listener(routing(payload))
      })
    },
    credentials: {
      async reveal(providerId, request) {
        return record(await requestProvider(rpc, 'onboarding.provider.credential.reveal', { providerId }, options(request?.signal)))
      },
      async clear(providerId, request) {
        return record(await requestProvider(rpc, 'onboarding.provider.credential.clear', { providerId }, options(request?.signal)))
      },
    },
  }
}
