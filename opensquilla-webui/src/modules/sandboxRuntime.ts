import type { InjectionKey } from 'vue'

import type {
  SandboxCapabilityReport,
  SandboxPolicy,
  SandboxPolicyDefaults,
  SandboxRunMode,
  SandboxRuntimeComponentId,
  SandboxRuntimeOperation,
  SandboxRuntimePackStatus,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

export interface SandboxRequestOptions {
  readonly timeoutMs?: number
  readonly signal?: AbortSignal
}

export interface SandboxReadinessOptions extends SandboxRequestOptions {
  readonly refreshCapability?: boolean
}

export type SandboxSetupOutcome =
  | 'idle'
  | 'ready'
  | 'in_progress'
  | 'cancelled'
  | 'failed'
  | 'verification_failed'

export interface SandboxReadinessState {
  readonly status: SandboxSetupStatusPayload | null
  readonly capability: SandboxCapabilityReport | null
}

export interface SandboxSetupResult extends SandboxReadinessState {
  readonly ready: boolean
  readonly outcome: Exclude<SandboxSetupOutcome, 'idle'>
}

export interface SandboxRunModePreference {
  readonly runMode: SandboxRunMode
  readonly source?: string
}

export interface SandboxSettingsSnapshot {
  readonly policy: SandboxPolicy
  readonly defaults: Partial<SandboxPolicyDefaults>
  readonly preference: SandboxRunModePreference
}

export type SandboxRuntimeActionReceipt =
  | { readonly kind: 'operation'; readonly operation: SandboxRuntimeOperation }
  | { readonly kind: 'status'; readonly status: SandboxRuntimePackStatus }

export interface SandboxResumeResult {
  readonly sessionKey: string
  readonly resumed: boolean
  readonly autonomousPaused: boolean
}

export type SandboxErrorCode =
  | 'unsupported'
  | 'forbidden'
  | 'conflict'
  | 'setup_required'
  | 'busy'
  | 'unavailable'
  | 'invalid'
  | 'aborted'
  | 'failed'

export class SandboxError extends Error {
  readonly code: SandboxErrorCode
  readonly retryable: boolean
  readonly details?: unknown
  readonly currentPolicy?: SandboxPolicy

  constructor(
    code: SandboxErrorCode,
    message: string,
    options: {
      retryable?: boolean
      details?: unknown
      currentPolicy?: SandboxPolicy
      cause?: unknown
    } = {},
  ) {
    super(message)
    if (options.cause !== undefined) {
      Object.defineProperty(this, 'cause', {
        configurable: true,
        value: options.cause,
      })
    }
    this.name = 'SandboxError'
    this.code = code
    this.retryable = options.retryable === true
    this.details = options.details
    this.currentPolicy = options.currentPolicy
  }
}

export interface SandboxReadiness {
  /** Read-only setup and capability projection. Never starts setup. */
  readiness(options?: SandboxReadinessOptions): Promise<SandboxReadinessState>

  /** Starts setup at most once, then verifies the authoritative capability. */
  ensureReady(options?: SandboxRequestOptions): Promise<SandboxSetupResult>
}

export interface SandboxPolicyAdministration {
  loadSettings(options?: SandboxRequestOptions): Promise<SandboxSettingsSnapshot>
  updatePolicy(
    basePolicyVersion: number,
    policy: SandboxPolicy,
    options?: SandboxRequestOptions,
  ): Promise<SandboxPolicy>
}

export interface SandboxPreference {
  preference(options?: SandboxRequestOptions): Promise<SandboxRunModePreference>
  selectMode(
    mode: SandboxRunMode,
    options?: SandboxRequestOptions,
  ): Promise<SandboxRunModePreference>
  onPreferenceChanged(
    handler: (preference: SandboxRunModePreference) => void,
  ): () => void
}

export interface SandboxRuntimeManagement {
  /** Returns null only when the optional runtime-management capability is absent. */
  runtimeStatus(options?: SandboxRequestOptions): Promise<SandboxRuntimePackStatus | null>
  installRuntime(
    componentId: SandboxRuntimeComponentId,
    options?: SandboxRequestOptions,
  ): Promise<SandboxRuntimeActionReceipt>
  cancelRuntime(
    componentId: SandboxRuntimeComponentId,
    operationId: string,
    options?: SandboxRequestOptions,
  ): Promise<SandboxRuntimeActionReceipt>
  removeRuntime(
    componentId: SandboxRuntimeComponentId,
    options?: SandboxRequestOptions,
  ): Promise<SandboxRuntimeActionReceipt>
  discardRuntimeDownload(
    componentId: SandboxRuntimeComponentId,
    options?: SandboxRequestOptions,
  ): Promise<SandboxRuntimeActionReceipt>
}

export interface SandboxSessionRecovery {
  resumeSession(
    sessionKey: string,
    options?: SandboxRequestOptions,
  ): Promise<SandboxResumeResult>
}

export type SandboxSettingsRuntime = SandboxReadiness
  & SandboxPolicyAdministration
  & SandboxPreference
  & SandboxRuntimeManagement

export type SandboxChatRuntime = SandboxReadiness
  & SandboxPreference
  & SandboxSessionRecovery

/** Composition Interface; callers depend on the narrow facet they consume. */
export type SandboxRuntime = SandboxSettingsRuntime & SandboxChatRuntime

export const SANDBOX_RUNTIME_KEY: InjectionKey<SandboxRuntime> = Symbol('SandboxRuntime')
