import type { InjectionKey } from 'vue'
import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxPolicyDefaults,
  SandboxRunMode,
  SandboxRuntimeComponentId,
  SandboxRuntimePackStatus,
  SandboxSetupStatusPayload,
  SandboxTokenRecord,
} from '@/types/sandbox'

export interface SandboxRuntime {
  status(): Promise<Record<string, unknown>>
  setupStatus(): Promise<SandboxSetupStatusPayload | null>
  ensureSetup(): Promise<SandboxSetupStatusPayload | null>
  capability(options?: { refresh?: boolean }): Promise<SandboxCapabilityReport>
  policy(): Promise<SandboxPolicy>
  policyDefaults(): Promise<Partial<SandboxPolicyDefaults>>
  updatePolicy(basePolicyVersion: number, policy: SandboxPolicy): Promise<SandboxPolicy>
  runModePreference(): Promise<{ runMode: SandboxRunMode; source?: string }>
  setRunMode(mode: SandboxRunMode): Promise<{ runMode: SandboxRunMode; source?: string }>
  subscribeRunModePreferenceChanged(handler: (payload: { runMode: SandboxRunMode; source?: string }) => void): () => void
  runtimeStatus(): Promise<SandboxRuntimePackStatus | null>
  installRuntime(componentId: SandboxRuntimeComponentId): Promise<unknown>
  cancelRuntime(componentId: SandboxRuntimeComponentId, operationId: string): Promise<unknown>
  removeRuntime(componentId: SandboxRuntimeComponentId): Promise<unknown>
  discardRuntimeDownload(componentId: SandboxRuntimeComponentId): Promise<unknown>
  listTokens(): Promise<{ tokens: SandboxTokenRecord[] }>
  createToken(name: string, hostExecute?: boolean): Promise<{ token: string; record: SandboxTokenRecord }>
  revokeToken(publicId: string): Promise<{ publicId: string; revoked: boolean }>
  resume(sessionKey: string): Promise<{ sessionKey: string; resumed: boolean; autonomousPaused: boolean }>
}

export const SANDBOX_RUNTIME_KEY: InjectionKey<SandboxRuntime> = Symbol('SandboxRuntime')
