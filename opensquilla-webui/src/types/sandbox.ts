export type SandboxRunMode = 'safe' | 'full'
export type SandboxSetupState = 'not_setup' | 'setting_up' | 'ready' | 'failed' | 'unavailable'

export interface SandboxSetupStatusPayload {
  state: SandboxSetupState
  platform: string
  message: string
  requiresAdmin: boolean
  detail?: string
}

export const SANDBOX_RUN_MODES: readonly SandboxRunMode[] = ['safe', 'full']

export function isSandboxRunMode(value: unknown): value is SandboxRunMode {
  return value === 'safe' || value === 'full'
}

export function isRecognizedSandboxRunMode(value: unknown): boolean {
  return isSandboxRunMode(value)
    || value === 'standard'
    || value === 'trusted'
    || value === 'managed'
    || value === 'bypass'
}

export function normalizeSandboxRunMode(value: unknown, fallback: SandboxRunMode = 'safe'): SandboxRunMode {
  if (value === 'full' || value === 'bypass') return 'full'
  if (isRecognizedSandboxRunMode(value)) return 'safe'
  return fallback
}

export interface SandboxCapabilityReport {
  available: boolean
  backend: string
  platform: string
  code: string
  reason: string
  setupSupported: boolean
  restartRequired: boolean
  probeVersion: number
  capabilities: string[]
}

export interface SandboxFilePolicy {
  customDenyWritePaths: string[]
  recursiveDeleteBackupEnabled: boolean
  backupQuotaBytes: number
}

export interface SandboxCommandPolicy {
  requireApprovalPrefixes: string[][]
  autoAllowPrefixes: string[][]
  systemTools: 'auto' | 'prompt' | 'disabled'
}

export interface SandboxNetworkPolicy {
  blockAllNetwork: boolean
  allowDomains: string[]
  denyDomains: string[]
}

export interface SandboxRuntimePolicy {
  enabled: boolean
  python: boolean
  node: boolean
  gitBash: boolean
}

export interface SandboxPolicy {
  schemaVersion: 2
  policyVersion: number
  files: SandboxFilePolicy
  commands: SandboxCommandPolicy
  network: SandboxNetworkPolicy
  runtimes: SandboxRuntimePolicy
}

export interface SandboxTokenRecord {
  publicId: string
  name: string
  capabilities: string[]
  createdAt: number
  lastUsedAt: number | null
  lastPeer: string | null
}

export interface SandboxRuntimeVersion {
  version: string
  available: boolean
}

export type SandboxRuntimeComponentId = 'python' | 'node' | 'gitBash'
export type SandboxRuntimeAvailability = 'unsupported' | 'missing' | 'ready' | 'corrupt'
export type SandboxRuntimeOperationState =
  | 'queued'
  | 'downloading'
  | 'verifying'
  | 'extracting'
  | 'probing'
  | 'activating'
  | 'cancelling'
  | 'removing'
  | 'completed'
  | 'cancelled'
  | 'failed'
  | 'interrupted'
export type SandboxRuntimeSource = 'oss' | 'github'

export interface SandboxRuntimeError {
  code?: string
  message: string
  retryable?: boolean
  source?: SandboxRuntimeSource | null
}

export interface SandboxRuntimeOperation {
  operationId: string
  componentId: SandboxRuntimeComponentId
  kind: 'install' | 'remove'
  state: SandboxRuntimeOperationState
  source: SandboxRuntimeSource | null
  downloadedBytes: number
  totalBytes: number | null
  progressPercent: number
  startedAtMs: number
  updatedAtMs: number
  error: SandboxRuntimeError | null
}

export interface SandboxRuntimeComponentStatus {
  componentId: SandboxRuntimeComponentId
  availability: SandboxRuntimeAvailability
  catalogVersion: string | null
  activeVersion: string | null
  installedBytes: number | null
  removable: boolean
  resumeAvailable: boolean
  resumeBytes: number
  operation: SandboxRuntimeOperation | null
  lastError: SandboxRuntimeError | null
}

export interface SandboxRuntimePackStatus {
  schemaVersion: 1
  managementSupported: boolean
  target: string | null
  catalogVersion: string | null
  sourceOrder: SandboxRuntimeSource[]
  components: SandboxRuntimeComponentStatus[]
  nextPollAfterMs: number
}

export interface SandboxPolicyDefaults {
  builtinDenyWritePaths: string[]
  runtimeTarget: string | null
  runtimeVersions: Partial<Record<'python' | 'node' | 'gitBash', SandboxRuntimeVersion>>
}
