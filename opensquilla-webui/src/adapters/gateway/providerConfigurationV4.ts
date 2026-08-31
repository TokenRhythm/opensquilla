import type { RpcCallOptions, RpcEventHandler } from '@/lib/rpc'
import type { ModelRoutingSnapshot, ProviderConfiguration, ProviderDescriptor } from '@/modules/providerConfiguration'
import { MODELS_ROUTING_GET_METHOD } from '@/contracts/generated/v4/modelsRoutingGet'
import { validateResult as validateModelsRoutingGetResult } from '@/contracts/generated/v4/modelsRoutingGetValidators.mjs'

interface RpcTransport { request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T> }
interface EventTransport { subscribe(event: string, handler: RpcEventHandler): { close(): void } }
const options = (signal?: AbortSignal): RpcCallOptions => ({ timeoutMs: 15_000, timeoutAction: 'reject', abortAction: 'reject', ...(signal ? { signal } : {}) })
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

function providers(value: unknown): ProviderDescriptor[] {
  const raw = record(value)
  const list = Array.isArray(raw.providers) ? raw.providers : Array.isArray(value) ? value : []
  return list.filter(item => item && typeof item === 'object').map(item => {
    const source = item as Record<string, unknown>
    return {
      ...source,
      id: String(source.id ?? source.providerId ?? source.provider_id ?? ''),
      ...(typeof source.label === 'string' ? { label: source.label } : {}),
      ...(Array.isArray(source.models) ? { models: source.models.filter(model => typeof model === 'string') as string[] } : {}),
    }
  }).filter(item => item.id)
}

function routing(value: unknown): ModelRoutingSnapshot {
  const source = record(value)
  const nested = record(source.routing ?? source.modelRouting ?? source.model_routing)
  return { ...nested, ...source, mode: String(nested.mode ?? source.mode ?? 'direct') }
}

export function createV4ProviderConfiguration(rpc: RpcTransport, _events?: EventTransport): ProviderConfiguration {
  return {
    async list(request) { return providers(await rpc.request('models.list', undefined, options(request?.signal))) },
    async status(request) { return record(await rpc.request('providers.status', undefined, options(request?.signal))) },
    async getRouting(request) {
      const result = await rpc.request(MODELS_ROUTING_GET_METHOD, undefined, options(request?.signal))
      if (!validateModelsRoutingGetResult(result)) throw new Error(`${MODELS_ROUTING_GET_METHOD} returned an invalid response`)
      return routing(result)
    },
    async setRouting(input, request) { return routing(await rpc.request('models.routing.set', input, options(request?.signal))) },
  }
}
