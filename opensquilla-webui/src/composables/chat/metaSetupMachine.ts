import type {
  MetaSetupJob,
  MetaSetupProviderHandoff,
  MetaSetupReadiness,
  MetaSetupRetryMode,
  MetaSetupState,
} from '@/types/metaSetup'

// Deterministic setup policy belongs here. Keep Vue refs, timers, RPC calls,
// browser storage, and user-visible side effects in their adapter/repository.
const PROVIDER_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/
const CLIENT_REQUEST_ID_PATTERN = /^\S{1,256}$/

export const META_SETUP_PROVIDER_HANDOFF_TTL_MS = 15 * 60 * 1000

export function normalizeMetaLaunchText(name: string, candidate?: string): string {
  const legacy = `/meta ${name}`
  const launchText = String(candidate || '').trim()
  if (launchText === legacy) return launchText
  const suffix = launchText.startsWith(legacy) ? launchText.slice(legacy.length) : ''
  if (/^\s+--(?:\s+[\s\S]*)?$/.test(suffix)) return launchText
  return legacy
}

export function availableMetaSetupActionIds(readiness: MetaSetupReadiness): string[] {
  return (readiness.setup_actions || [])
    .filter(action => action.available !== false && Boolean(action.id))
    .map(action => action.id)
}

export function normalizeMetaSetupProviderId(candidate: unknown): string {
  const providerId = typeof candidate === 'string' ? candidate.trim().toLowerCase() : ''
  return PROVIDER_ID_PATTERN.test(providerId) ? providerId : ''
}

export function availableMetaSetupProviderIds(readiness: MetaSetupReadiness): string[] {
  const providerIds = (readiness.manual_setup_actions || [])
    .filter(action => action.kind === 'provider_connection' && action.available !== false)
    .map(action => normalizeMetaSetupProviderId(action.provider_id))
    .filter(Boolean)
  return [...new Set(providerIds)]
}

export function normalizeMetaSetupClientRequestId(candidate: unknown): string {
  const clientRequestId = typeof candidate === 'string' ? candidate.trim() : ''
  return CLIENT_REQUEST_ID_PATTERN.test(clientRequestId) ? clientRequestId : ''
}

export function validMetaSetupProviderHandoff(
  candidate: unknown,
  readiness: MetaSetupReadiness,
  nowMs = Date.now(),
): MetaSetupProviderHandoff | undefined {
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return undefined
  const value = candidate as Partial<MetaSetupProviderHandoff>
  const providerId = normalizeMetaSetupProviderId(value.providerId)
  const clientRequestId = normalizeMetaSetupClientRequestId(value.clientRequestId)
  const startedAtMs = value.startedAtMs
  if (typeof startedAtMs !== 'number') return undefined
  const ageMs = nowMs - startedAtMs
  if (
    value.kind !== 'provider_settings'
    || !providerId
    || !clientRequestId
    || !availableMetaSetupProviderIds(readiness).includes(providerId)
    || !Number.isFinite(startedAtMs)
    || ageMs < 0
    || ageMs > META_SETUP_PROVIDER_HANDOFF_TTL_MS
  ) {
    return undefined
  }
  return { kind: 'provider_settings', providerId, startedAtMs, clientRequestId }
}

export function metaSetupErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error || 'Unknown setup error')
}

export function isMissingMetaSetupJobError(error: unknown): boolean {
  return /(?:not found|404|unknown (?:meta )?setup job|setup job (?:is )?unknown)/i
    .test(metaSetupErrorMessage(error))
}

export function isBusyMetaSetupState(state: MetaSetupState): boolean {
  return state.phase === 'installing' || state.phase === 'verifying'
}

export function metaSetupResumeRequestId(
  current: MetaSetupState | null | undefined,
): string {
  return normalizeMetaSetupClientRequestId(
    current?.resumeRequestId || current?.providerHandoff?.clientRequestId,
  )
}

export function createMetaSetupConfirmationState(
  name: string,
  readiness: MetaSetupReadiness,
  sessionKey: string,
  launchText: string,
): MetaSetupState {
  const actionIds = availableMetaSetupActionIds(readiness)
  const providerIds = availableMetaSetupProviderIds(readiness)
  if (actionIds.length === 0 && providerIds.length === 0) {
    return {
      name,
      sessionKey,
      launchText: normalizeMetaLaunchText(name, launchText),
      phase: 'blocked',
      readiness,
      actionIds,
      completedActions: [],
      error: readiness.reasons?.join('; ') || '',
      blockedReason: 'no_actions',
      retryMode: 'readiness',
    }
  }
  return {
    name,
    sessionKey,
    launchText: normalizeMetaLaunchText(name, launchText),
    phase: 'confirm',
    readiness,
    actionIds,
    completedActions: [],
    retryMode: actionIds.length === 0 ? 'readiness' : undefined,
  }
}

export type MetaSetupMachineEvent =
  | { type: 'failed'; error: string; retryMode: MetaSetupRetryMode }
  | { type: 'install_started' }
  | {
    type: 'verification_started'
    readiness?: MetaSetupReadiness
    clearSuppressAutoResume?: boolean
  }
  | { type: 'launch_queued' }
  | { type: 'session_changed'; clearRetryMode?: boolean }
  | { type: 'provider_handoff_started'; handoff: MetaSetupProviderHandoff }
  | { type: 'provider_handoff_cancelled' }

export function transitionMetaSetupState(
  current: MetaSetupState,
  event: MetaSetupMachineEvent,
): MetaSetupState {
  if (event.type === 'failed') {
    return {
      ...current,
      phase: 'failed',
      error: event.error,
      retryMode: event.retryMode,
    }
  }
  if (event.type === 'install_started') {
    return {
      ...current,
      phase: 'installing',
      message: '',
      error: '',
      retryMode: undefined,
    }
  }
  if (event.type === 'verification_started') {
    const next = {
      ...current,
      phase: 'verifying' as const,
      ...(event.readiness ? { readiness: event.readiness } : {}),
      ...(event.clearSuppressAutoResume ? { suppressAutoResume: undefined } : {}),
      message: '',
      error: '',
      retryMode: undefined,
    }
    return next
  }
  if (event.type === 'launch_queued') {
    return {
      ...current,
      phase: 'verifying',
      message: '',
      error: '',
      retryMode: undefined,
    }
  }
  if (event.type === 'session_changed') {
    return {
      ...current,
      phase: 'blocked',
      blockedReason: 'session_changed',
      ...(event.clearRetryMode ? { retryMode: undefined } : {}),
    }
  }
  if (event.type === 'provider_handoff_started') {
    const { suppressAutoResume: _suppressAutoResume, ...resumable } = current
    return {
      ...resumable,
      resumeRequestId: event.handoff.clientRequestId,
      providerHandoff: event.handoff,
    }
  }
  const { providerHandoff: _providerHandoff, ...next } = current
  return next
}

export function failMetaSetupState(
  current: MetaSetupState,
  error: string,
  retryMode: MetaSetupRetryMode,
): MetaSetupState {
  return transitionMetaSetupState(current, { type: 'failed', error, retryMode })
}

export type MetaSetupJobProjection = {
  kind: 'completed' | 'blocked' | 'failed' | 'active'
  state: MetaSetupState
}

export function projectMetaSetupJob(
  current: MetaSetupState,
  job: MetaSetupJob,
  launchText: string,
): MetaSetupJobProjection {
  const completedActions = [...(job.completed_actions || [])]
  const remainingActionIds = (job.action_ids || current.actionIds)
    .filter(actionId => !completedActions.includes(actionId))
  const readiness = job.readiness || current.readiness

  if (job.status === 'completed' || job.phase === 'completed') {
    return {
      kind: 'completed',
      state: {
        ...current,
        name: job.name,
        launchText,
        phase: 'verifying',
        readiness,
        jobId: job.job_id,
        jobStatus: job.status,
        message: job.message || '',
        currentAction: '',
        downloadedBytes: job.downloaded_bytes || 0,
        downloadTotalBytes: job.download_total_bytes || 0,
        completedActions,
        error: '',
      },
    }
  }

  if (job.status === 'blocked' || job.phase === 'blocked') {
    const next = createMetaSetupConfirmationState(
      job.name,
      readiness,
      job.sessionKey,
      launchText,
    )
    return {
      kind: 'blocked',
      state: {
        ...next,
        jobId: job.job_id,
        jobStatus: job.status,
        message: job.message || '',
        currentAction: '',
        downloadedBytes: job.downloaded_bytes || 0,
        downloadTotalBytes: job.download_total_bytes || 0,
        completedActions,
        error: job.error || next.error || '',
        blockedReason: next.phase === 'blocked' ? 'requirements_remaining' : undefined,
        providerHandoff: current.providerHandoff,
        resumeRequestId: metaSetupResumeRequestId(current) || undefined,
      },
    }
  }

  if (job.status === 'failed' || job.phase === 'failed') {
    return {
      kind: 'failed',
      state: {
        ...current,
        name: job.name,
        launchText,
        phase: 'failed',
        actionIds: remainingActionIds,
        jobId: job.job_id,
        jobStatus: job.status,
        message: job.message || '',
        currentAction: '',
        downloadedBytes: job.downloaded_bytes || 0,
        downloadTotalBytes: job.download_total_bytes || 0,
        completedActions,
        error: job.error || job.message || 'Setup failed',
        retryMode: remainingActionIds.length ? 'install' : 'status',
      },
    }
  }

  return {
    kind: 'active',
    state: {
      ...current,
      name: job.name,
      launchText,
      phase: job.phase === 'verifying' ? 'verifying' : 'installing',
      readiness,
      actionIds: job.action_ids || current.actionIds,
      jobId: job.job_id,
      jobStatus: job.status,
      message: job.message || '',
      currentAction: job.current_action || '',
      downloadedBytes: job.downloaded_bytes || 0,
      downloadTotalBytes: job.download_total_bytes || 0,
      completedActions,
      error: '',
      retryMode: undefined,
    },
  }
}
