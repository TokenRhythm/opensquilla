export type MetaSetupPhase =
  | 'confirm'
  | 'installing'
  | 'verifying'
  | 'failed'
  | 'blocked'

export type MetaSetupRetryMode = 'install' | 'status' | 'launch' | 'readiness' | 'discard'

export interface MetaSetupAction {
  id: string
  skill?: string
  install_id?: string
  kind?: string
  label?: string
  bins?: string[]
  available?: boolean
  reason?: string
  version?: string
  download_size_bytes?: number | null
  download_size_is_minimum?: boolean
  source?: string
  license?: string
  requires_admin?: boolean
}

export interface MetaSetupManualAction {
  id: string
  kind: 'provider_connection' | string
  provider_id?: string
  label?: string
  capability_ids?: string[]
  reason_code?: string
  recommended?: boolean
  available?: boolean
  reason?: string
}

export interface MetaSetupProviderHandoff {
  kind: 'provider_settings'
  providerId: string
  startedAtMs: number
  clientRequestId: string
}

export interface MetaSetupReadiness {
  ready?: boolean
  status?: 'ready' | 'needs_setup' | string
  missing_bins?: string[]
  missing_env?: string[]
  missing_env_any?: string[][]
  missing_skills?: string[]
  missing_capabilities?: string[]
  missing_provider_capabilities?: string[]
  reasons?: string[]
  setup_actions?: MetaSetupAction[]
  manual_setup_actions?: MetaSetupManualAction[]
}

export interface MetaSetupJob {
  job_id: string
  name: string
  sessionKey: string
  action_ids: string[]
  status: 'queued' | 'running' | 'completed' | 'failed' | 'blocked' | string
  phase: 'queued' | 'installing' | 'verifying' | 'completed' | 'failed' | 'blocked' | string
  message?: string
  current_action?: string
  downloaded_bytes?: number
  download_total_bytes?: number
  completed_actions?: string[]
  error?: string
  started_at_ms?: number
  finished_at_ms?: number
  readiness?: MetaSetupReadiness | null
}

export interface MetaSetupState {
  name: string
  sessionKey: string
  launchText?: string
  phase: MetaSetupPhase
  readiness: MetaSetupReadiness
  actionIds: string[]
  jobId?: string
  jobStatus?: string
  message?: string
  currentAction?: string
  downloadedBytes?: number
  downloadTotalBytes?: number
  completedActions: string[]
  error?: string
  retryMode?: MetaSetupRetryMode
  providerHandoff?: MetaSetupProviderHandoff
  /** Stable ingress id retained until the resumed hidden launch is accepted. */
  resumeRequestId?: string
  /** A stale provider detour keeps its draft identity but requires an explicit retry. */
  suppressAutoResume?: boolean
  blockedReason?:
    | 'no_actions'
    | 'requirements_remaining'
    | 'session_changed'
    | 'launch_failed'
}

export interface MetaSetupInstallResponse {
  ok?: boolean
  job?: MetaSetupJob
  already_ready?: boolean
  readiness?: MetaSetupReadiness
  error?: string
}

export interface MetaSetupStatusResponse {
  ok?: boolean
  job?: MetaSetupJob
  error?: string
}

export interface MetaSetupRunResponse {
  ok?: boolean
  setup_required?: boolean
  readiness?: MetaSetupReadiness
  error?: string
}

export interface MetaSetupPlanResponse {
  ok?: boolean
  name?: string
  readiness?: MetaSetupReadiness
  error?: string
}
