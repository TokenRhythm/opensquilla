import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import type { RpcRequester as RpcTransport } from './privateTransports'
import { readTransportFailure } from './transportTypes'
import type {
  AppSettings,
  EffectiveSettings,
  SettingChange,
  SettingsMutation,
  SettingsObject,
  SettingsValue,
} from '@/modules/appSettings'
import { AppSettingsError } from '@/modules/appSettings'
import { CONFIG_GET_METHOD } from '@/contracts/generated/v4/configGet'
import { validateResult as validateConfigGetResult } from '@/contracts/generated/v4/configGetValidators.mjs'
import { CONFIG_EFFECTIVE_METHOD } from '@/contracts/generated/v4/configEffective'
import { validateResult as validateConfigEffectiveResult } from '@/contracts/generated/v4/configEffectiveValidators.mjs'
import { CONFIG_PATCH_METHOD } from '@/contracts/generated/v4/configPatch'
import { validateResult as validateConfigPatchResult } from '@/contracts/generated/v4/configPatchValidators.mjs'
import { CONFIG_PATCH_SAFE_METHOD } from '@/contracts/generated/v4/configPatchSafe'
import { validateResult as validateConfigPatchSafeResult } from '@/contracts/generated/v4/configPatchSafeValidators.mjs'

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function settings(value: unknown): SettingsObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${CONFIG_GET_METHOD} returned a non-object public config`)
  }
  return value as SettingsObject
}

function effective(value: unknown): EffectiveSettings {
  const raw = object(value)
  const fields = object(raw.fields)
  for (const [path, field] of Object.entries(fields)) {
    const record = object(field)
    if (!('value' in record) || typeof record.source !== 'string') {
      throw new Error(`config.effective returned an invalid field: ${path}`)
    }
  }
  return { fields: fields as EffectiveSettings['fields'] }
}

function mutation(value: unknown): SettingsMutation {
  const raw = object(value)
  const result: Record<string, unknown> = { ...raw }
  const values = raw.values ?? raw.config ?? raw.settings
  if (values && typeof values === 'object' && !Array.isArray(values)) {
    result.values = values as SettingsObject
  } else {
    delete result.values
  }
  if (Number.isInteger(raw.revision)) result.revision = raw.revision as number
  if (typeof raw.restartRequired === 'boolean') result.restartRequired = raw.restartRequired
  else if (typeof raw.restart_required === 'boolean') result.restartRequired = raw.restart_required
  if (Array.isArray(raw.patched)) result.patched = raw.patched.filter(item => typeof item === 'string')
  if (Array.isArray(raw.linked)) result.linked = raw.linked.filter(item => typeof item === 'string')
  return result as SettingsMutation
}

function patchMap(changes: readonly SettingChange[]): Record<string, SettingsValue> {
  const patches: Record<string, SettingsValue> = {}
  for (const change of changes) {
    const path = change?.path?.trim()
    if (!path) throw new Error('Setting paths must not be empty')
    if (Object.prototype.hasOwnProperty.call(patches, path)) {
      throw new Error(`Duplicate setting path: ${path}`)
    }
    patches[path] = change.value
  }
  return patches
}

function readOptions(signal?: AbortSignal): RpcCallOptions {
  return { timeoutMs: 10_000, timeoutAction: 'reconnect', abortAction: 'reject', ...(signal ? { signal } : {}) }
}

function mutationOptions(signal?: AbortSignal): RpcCallOptions {
  return { timeoutMs: 15_000, timeoutAction: 'reject', abortAction: 'reject', ...(signal ? { signal } : {}) }
}

function mapSettingsError(error: unknown): AppSettingsError {
  if (error instanceof AppSettingsError) return error
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
  return new AppSettingsError(domainCode, failure.message)
}

async function requestSettings<T>(
  rpc: RpcTransport,
  method: string,
  params: Record<string, unknown> | undefined,
  requestOptions: RpcCallOptions,
): Promise<T> {
  try {
    return await rpc.request<T>(method, params, requestOptions)
  } catch (error) {
    throw mapSettingsError(error)
  }
}

export function createV4AppSettings(rpc: RpcTransport): AppSettings {
  return {
    async readAll(request) {
      const result = await requestSettings(rpc, CONFIG_GET_METHOD, undefined, readOptions(request?.signal))
      if (!validateConfigGetResult(result) || !result || typeof result !== 'object' || Array.isArray(result)) {
        throw new Error(`${CONFIG_GET_METHOD} returned an invalid response`)
      }
      return settings(result)
    },
    async read(path, request) {
      const normalizedPath = path.trim()
      if (!normalizedPath) throw new Error('Setting path must not be empty')
      const result = await requestSettings(rpc, CONFIG_GET_METHOD, { path: normalizedPath }, readOptions(request?.signal))
      if (!validateConfigGetResult(result)) throw new Error(`${CONFIG_GET_METHOD} returned an invalid response`)
      return result as SettingsValue | null
    },
    async readEffective(request) {
      const result = await requestSettings(rpc, CONFIG_EFFECTIVE_METHOD, undefined, readOptions(request?.signal))
      if (!validateConfigEffectiveResult(result)) throw new Error(`${CONFIG_EFFECTIVE_METHOD} returned an invalid response`)
      return effective(result)
    },
    async patch(patches, request) {
      const result = await requestSettings(rpc, CONFIG_PATCH_METHOD, { patches: patchMap(patches) }, mutationOptions(request?.signal))
      if (!validateConfigPatchResult(result)) throw new Error(`${CONFIG_PATCH_METHOD} returned an invalid response`)
      return mutation(result)
    },
    async patchSafe(patches, request) {
      const result = await requestSettings(rpc, CONFIG_PATCH_SAFE_METHOD, { patches: patchMap(patches) }, mutationOptions(request?.signal))
      if (!validateConfigPatchSafeResult(result)) throw new Error(`${CONFIG_PATCH_SAFE_METHOD} returned an invalid response`)
      return mutation(result)
    },
    async merge(patch, request) {
      if (!patch || typeof patch !== 'object' || Array.isArray(patch)) {
        throw new Error('Config merge patch must be an object')
      }
      const result = await requestSettings(rpc, CONFIG_PATCH_METHOD, { patch }, mutationOptions(request?.signal))
      if (!validateConfigPatchResult(result)) throw new Error(`${CONFIG_PATCH_METHOD} returned an invalid response`)
      return mutation(result)
    },
  }
}
