import type { RpcCallOptions } from '@/lib/rpc'
import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxPolicyDefaults,
  SandboxRunMode,
  SandboxRuntimePackStatus,
  SandboxSetupStatusPayload,
  SandboxTokenRecord,
} from '@/types/sandbox'
import type { SandboxRuntime } from '@/modules/sandboxRuntime'

interface SandboxTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready?: (options?: { timeoutMs?: number; signal?: AbortSignal }) => Promise<void>
  subscribe?: (event: string, handler: (payload: unknown) => void) => { close(): void }
}

const METHODS = {
  status: 'sandbox.status',
  setupStatus: 'sandbox.setup.status',
  ensureSetup: 'sandbox.setup.ensure',
  capability: 'sandbox.capability.status',
  policy: 'sandbox.policy.get',
  policyDefaults: 'sandbox.policy.defaults',
  policyUpdate: 'sandbox.policy.update',
  runModeGet: 'sandbox.run_mode.preference.get',
  runModeSet: 'sandbox.run_mode.preference.set',
  runtimeStatus: 'sandbox.runtime.status',
  runtimeInstall: 'sandbox.runtime.install',
  runtimeCancel: 'sandbox.runtime.cancel',
  runtimeRemove: 'sandbox.runtime.remove',
  runtimeDiscard: 'sandbox.runtime.discard_download',
  tokensList: 'sandbox.tokens.list',
  tokensCreate: 'sandbox.tokens.create',
  tokensRevoke: 'sandbox.tokens.revoke',
  resume: 'sandbox.resume',
} as const

function options(): RpcCallOptions {
  return { timeoutMs: 15_000, timeoutAction: 'reject', abortAction: 'reject' }
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function setupStatus(value: unknown): SandboxSetupStatusPayload | null {
  const raw = record(value)
  const state = raw.state
  if (!['not_setup', 'setting_up', 'ready', 'failed', 'unavailable'].includes(String(state))) return null
  return {
    state: state as SandboxSetupStatusPayload['state'],
    platform: String(raw.platform ?? ''),
    message: String(raw.message ?? ''),
    requiresAdmin: raw.requiresAdmin === true || raw.requires_admin === true,
    ...(typeof raw.detail === 'string' ? { detail: raw.detail } : {}),
  }
}

function runtimeStatus(value: unknown): SandboxRuntimePackStatus | null {
  const raw = record(value)
  const candidate: Record<string, unknown> = record(raw.status).schemaVersion
    ? record(raw.status)
    : raw
  if (candidate.schemaVersion !== 1 || !Array.isArray(candidate.components)) return null
  return candidate as unknown as SandboxRuntimePackStatus
}

export function createV4SandboxRuntime(transport: SandboxTransport): SandboxRuntime {
  const request = async <T>(method: string, params?: Record<string, unknown>, requestOptions?: { timeoutMs?: number; signal?: AbortSignal }) => {
    if (transport.ready) await transport.ready({ timeoutMs: requestOptions?.timeoutMs ?? 15_000, signal: requestOptions?.signal })
    return await transport.request<T>(method, params, {
      ...options(),
      ...(requestOptions?.timeoutMs !== undefined ? { timeoutMs: requestOptions.timeoutMs } : {}),
      ...(requestOptions?.signal ? { signal: requestOptions.signal } : {}),
    })
  }

  return {
    async status() { return record(await request(METHODS.status)) },
    async setupStatus() { return setupStatus(await request(METHODS.setupStatus)) },
    async ensureSetup() { return setupStatus(await request(METHODS.ensureSetup)) },
    async capability(requestOptions) {
      return await request<SandboxCapabilityReport>(METHODS.capability, requestOptions?.refresh ? { refresh: true } : undefined)
    },
    async policy() { return await request<SandboxPolicy>(METHODS.policy) },
    async policyDefaults() { return await request<Partial<SandboxPolicyDefaults>>(METHODS.policyDefaults) },
    async updatePolicy(basePolicyVersion, policy) {
      return await request<SandboxPolicy>(METHODS.policyUpdate, { basePolicyVersion, policy })
    },
    async runModePreference(requestOptions) {
      return await request<{ runMode: SandboxRunMode; source?: string }>(METHODS.runModeGet, undefined, requestOptions)
    },
    async setRunMode(mode, requestOptions) {
      return await request<{ runMode: SandboxRunMode; source?: string }>(METHODS.runModeSet, { runMode: mode }, requestOptions)
    },
    subscribeRunModePreferenceChanged(handler) {
      if (!transport.subscribe) return () => undefined
      const subscription = transport.subscribe('sandbox.run_mode.preference.changed', value => {
        const raw = record(value)
        if (raw.runMode === 'safe' || raw.runMode === 'full') {
          handler({ runMode: raw.runMode, ...(typeof raw.source === 'string' ? { source: raw.source } : {}) })
        }
      })
      return () => subscription.close()
    },
    async runtimeStatus() { return runtimeStatus(await request(METHODS.runtimeStatus)) },
    async installRuntime(componentId) { return await request(METHODS.runtimeInstall, { componentId }) },
    async cancelRuntime(componentId, operationId) { return await request(METHODS.runtimeCancel, { componentId, operationId }) },
    async removeRuntime(componentId) { return await request(METHODS.runtimeRemove, { componentId }) },
    async discardRuntimeDownload(componentId) { return await request(METHODS.runtimeDiscard, { componentId }) },
    async listTokens() { return await request<{ tokens: SandboxTokenRecord[] }>(METHODS.tokensList) },
    async createToken(name, hostExecute = true) {
      return await request<{ token: string; record: SandboxTokenRecord }>(METHODS.tokensCreate, { name, hostExecute })
    },
    async revokeToken(publicId) {
      return await request<{ publicId: string; revoked: boolean }>(METHODS.tokensRevoke, { publicId })
    },
    async resume(sessionKey) {
      return await request<{ sessionKey: string; resumed: boolean; autonomousPaused: boolean }>(METHODS.resume, { sessionKey })
    },
  }
}
