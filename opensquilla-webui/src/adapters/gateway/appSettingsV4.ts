import type { RpcCallOptions } from '@/lib/rpc'
import type { AppSettings, AppSettingsSnapshot, SettingsObject } from '@/modules/appSettings'
import { CONFIG_GET_METHOD } from '@/contracts/generated/v4/configGet'
import { validateResult as validateConfigGetResult } from '@/contracts/generated/v4/configGetValidators.mjs'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function snapshot(value: unknown): AppSettingsSnapshot {
  const raw = object(value)
  const values = object(raw.values ?? raw.config ?? raw.settings) as SettingsObject
  return {
    values,
    ...(Number.isInteger(raw.revision) ? { revision: raw.revision as number } : {}),
    ...(typeof raw.restartRequired === 'boolean' ? { restartRequired: raw.restartRequired } : {}),
  }
}

function options(signal?: AbortSignal): RpcCallOptions {
  return { timeoutMs: 15_000, timeoutAction: 'reject', abortAction: 'reject', ...(signal ? { signal } : {}) }
}

export function createV4AppSettings(rpc: RpcTransport): AppSettings {
  return {
    async get(path, request) {
      const result = await rpc.request(CONFIG_GET_METHOD, path ? { path } : undefined, options(request?.signal))
      if (!validateConfigGetResult(result)) throw new Error(`${CONFIG_GET_METHOD} returned an invalid response`)
      return snapshot(result)
    },
    async effective(request) {
      return snapshot(await rpc.request('config.effective', undefined, options(request?.signal)))
    },
    async patch(patches, request) {
      return snapshot(await rpc.request('config.patch', { patches }, options(request?.signal)))
    },
    async patchSafe(patches, request) {
      return snapshot(await rpc.request('config.patch.safe', { patches }, options(request?.signal)))
    },
  }
}
