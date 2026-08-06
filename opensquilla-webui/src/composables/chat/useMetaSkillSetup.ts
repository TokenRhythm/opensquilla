import { getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'

import type { RpcClientError } from '@/lib/rpc'
import type {
  MetaSetupInstallResponse,
  MetaSetupJob,
  MetaSetupPlanResponse,
  MetaSetupProviderHandoff,
  MetaSetupReadiness,
  MetaSetupRunResponse,
  MetaSetupState,
  MetaSetupStatusResponse,
} from '@/types/metaSetup'
import type { HiddenControlDispatchResult } from '@/types/chat'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
import {
  listPendingMetaDiscards,
  persistPendingMetaDiscard,
  removePendingMetaDiscard,
} from '@/utils/chat/metaDiscardOutbox'

type RpcClient = {
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  waitForConnection?: (timeoutMs?: number) => Promise<void>
}

export interface MetaSetupStorage {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
  removeItem: (key: string) => void
}

export type MetaDraftDiscardOutcome = 'discarded' | 'accepted' | 'unconfirmed'

export interface UseMetaSkillSetupOptions {
  rpc: RpcClient
  currentSessionKey: Ref<string>
  dispatchHidden: (
    providerText: string,
    displayText: string,
    clientRequestId?: string,
  ) => HiddenControlDispatchResult | Promise<HiddenControlDispatchResult>
  pollIntervalMs?: number
  storage?: MetaSetupStorage | null
  // Cancellation identities outlive a browser tab/Desktop restart. They are
  // minimal (session + request id only), so localStorage is appropriate.
  discardStorage?: MetaSetupStorage | null
  // ChatView defers recovery until its session event subscription is live.
  // Other callers retain the historical eager recovery behavior by default.
  autoRestore?: boolean
  // Return an unlaunched request to the composer when the user dismisses a
  // stable setup card. Hiding a running install must not duplicate its launch.
  restoreDraft?: (launchText: string, sessionKey: string) => void
  // Explicit "Not now" transfers the request back to the composer, so the
  // server setup outbox must stop recreating the dismissed card on reload.
  discardDraft?: (
    sessionKey: string,
    clientRequestId: string,
  ) => boolean | MetaDraftDiscardOutcome | Promise<boolean | MetaDraftDiscardOutcome>
  // A response-loss race can make a launch irrevocably accepted before the
  // user clicks "Not now". Surface that terminal state without restoring a
  // second sendable copy of the same paid request.
  onDraftAlreadyAccepted?: (sessionKey: string, clientRequestId: string) => void
  // Remove a browser-side hidden-control retry when the Gateway proves the
  // same stable identity was cancelled in another tab.
  forgetHiddenControl?: (sessionKey: string, clientRequestId: string) => void
}

const STORAGE_PREFIX = 'opensquilla.chat.metaSetupJob:'
const LAUNCH_STORAGE_PREFIX = 'opensquilla.chat.metaSetupLaunch:'
const MANUAL_STORAGE_PREFIX = 'opensquilla.chat.metaSetupManual:'
const DEFAULT_POLL_INTERVAL_MS = 850
const PROVIDER_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/
const CLIENT_REQUEST_ID_PATTERN = /^\S{1,256}$/

export const META_SETUP_PROVIDER_HANDOFF_TTL_MS = 15 * 60 * 1000

export function metaSetupStorageKey(sessionKey: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(sessionKey)}`
}

export function metaSetupLaunchStorageKey(sessionKey: string): string {
  return `${LAUNCH_STORAGE_PREFIX}${encodeURIComponent(sessionKey)}`
}

export function metaSetupManualStorageKey(sessionKey: string): string {
  return `${MANUAL_STORAGE_PREFIX}${encodeURIComponent(sessionKey)}`
}

function normalizeMetaLaunchText(name: string, candidate?: string): string {
  const legacy = `/meta ${name}`
  const launchText = String(candidate || '').trim()
  if (launchText === legacy) return launchText
  const suffix = launchText.startsWith(legacy) ? launchText.slice(legacy.length) : ''
  if (/^\s+--(?:\s+[\s\S]*)?$/.test(suffix)) return launchText
  return legacy
}

function availableActionIds(readiness: MetaSetupReadiness): string[] {
  return (readiness.setup_actions || [])
    .filter(action => action.available !== false && Boolean(action.id))
    .map(action => action.id)
}

function normalizeProviderId(candidate: unknown): string {
  const providerId = typeof candidate === 'string' ? candidate.trim().toLowerCase() : ''
  return PROVIDER_ID_PATTERN.test(providerId) ? providerId : ''
}

function availableProviderIds(readiness: MetaSetupReadiness): string[] {
  const providerIds = (readiness.manual_setup_actions || [])
    .filter(action => action.kind === 'provider_connection' && action.available !== false)
    .map(action => normalizeProviderId(action.provider_id))
    .filter(Boolean)
  return [...new Set(providerIds)]
}

function normalizeClientRequestId(candidate: unknown): string {
  const clientRequestId = typeof candidate === 'string' ? candidate.trim() : ''
  return CLIENT_REQUEST_ID_PATTERN.test(clientRequestId) ? clientRequestId : ''
}

function normalizeDiscardOutcome(candidate: unknown): MetaDraftDiscardOutcome {
  if (candidate === true || candidate === 'discarded') return 'discarded'
  if (candidate === 'accepted') return 'accepted'
  return 'unconfirmed'
}

function validProviderHandoff(
  candidate: unknown,
  readiness: MetaSetupReadiness,
  nowMs = Date.now(),
): MetaSetupProviderHandoff | undefined {
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return undefined
  const value = candidate as Partial<MetaSetupProviderHandoff>
  const providerId = normalizeProviderId(value.providerId)
  const clientRequestId = normalizeClientRequestId(value.clientRequestId)
  const startedAtMs = value.startedAtMs
  if (typeof startedAtMs !== 'number') return undefined
  const ageMs = nowMs - startedAtMs
  if (
    value.kind !== 'provider_settings'
    || !providerId
    || !clientRequestId
    || !availableProviderIds(readiness).includes(providerId)
    || !Number.isFinite(startedAtMs)
    || ageMs < 0
    || ageMs > META_SETUP_PROVIDER_HANDOFF_TTL_MS
  ) {
    return undefined
  }
  return { kind: 'provider_settings', providerId, startedAtMs, clientRequestId }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error || 'Unknown setup error')
}

function isMissingJobError(error: unknown): boolean {
  return /(?:not found|404|unknown (?:meta )?setup job|setup job (?:is )?unknown)/i
    .test(errorMessage(error))
}

function isBusyPhase(state: MetaSetupState): boolean {
  return state.phase === 'installing' || state.phase === 'verifying'
}

function defaultStorage(): MetaSetupStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

function defaultDiscardStorage(): MetaSetupStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function useMetaSkillSetup(options: UseMetaSkillSetupOptions) {
  const setupState = ref<MetaSetupState | null>(null)
  const pollIntervalMs = Math.max(750, Math.min(1000, options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS))
  const storage = options.storage === undefined ? defaultStorage() : options.storage
  const discardStorage = options.discardStorage === undefined
    ? defaultDiscardStorage()
    : options.discardStorage

  let operationToken = 0
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let installInFlight = false
  let cancelInFlight = false
  let disposed = false

  async function rpcCall<T>(method: string, params?: Record<string, unknown>): Promise<T> {
    if (options.rpc.waitForConnection) await options.rpc.waitForConnection(15_000)
    return options.rpc.call<T>(method, params)
  }

  function stopPolling(): void {
    if (pollTimer !== null) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  function beginOperation(): number {
    stopPolling()
    operationToken += 1
    return operationToken
  }

  function isCurrent(token: number): boolean {
    return !disposed && token === operationToken
  }

  function readPersistedJob(sessionKey: string): string {
    if (!storage || !sessionKey) return ''
    try {
      return String(storage.getItem(metaSetupStorageKey(sessionKey)) || '')
    } catch {
      return ''
    }
  }

  function persistJob(sessionKey: string, jobId: string): void {
    if (!storage || !sessionKey || !jobId) return
    try {
      storage.setItem(metaSetupStorageKey(sessionKey), jobId)
    } catch {
      // A blocked sessionStorage must not prevent setup from completing.
    }
  }

  function readPersistedLaunch(sessionKey: string): string {
    if (!storage || !sessionKey) return ''
    try {
      return String(storage.getItem(metaSetupLaunchStorageKey(sessionKey)) || '')
    } catch {
      return ''
    }
  }

  function persistLaunch(sessionKey: string, launchText: string): void {
    if (!storage || !sessionKey || !launchText) return
    try {
      storage.setItem(metaSetupLaunchStorageKey(sessionKey), launchText)
    } catch {
      // A blocked sessionStorage must not prevent setup from completing.
    }
  }

  function clearPersistedJobMarker(sessionKey: string): void {
    if (!storage || !sessionKey) return
    try {
      storage.removeItem(metaSetupStorageKey(sessionKey))
    } catch {
      // Best-effort cleanup only.
    }
  }

  function clearPersistedLaunch(sessionKey: string): void {
    if (!storage || !sessionKey) return
    try {
      storage.removeItem(metaSetupLaunchStorageKey(sessionKey))
    } catch {
      // Best-effort cleanup only.
    }
  }

  function clearPersistedManualSetup(sessionKey: string): void {
    if (!storage || !sessionKey) return
    try {
      storage.removeItem(metaSetupManualStorageKey(sessionKey))
    } catch {
      // Best-effort cleanup only.
    }
  }

  function confirmState(
    name: string,
    readiness: MetaSetupReadiness,
    sessionKey: string,
    launchText: string,
  ): MetaSetupState {
    const actionIds = availableActionIds(readiness)
    const providerIds = availableProviderIds(readiness)
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

  function persistSetupCheckpoint(current: MetaSetupState): boolean {
    // Persist only stable recovery inputs, never server-owned progress. Keeping
    // this checkpoint while a job is active lets the UI recover the original
    // readiness and launch after a Gateway restart invalidates the job id.
    if (!storage || !current.sessionKey) return false
    try {
      const providerHandoff = validProviderHandoff(
        current.providerHandoff,
        current.readiness,
      )
      const resumeRequestId = normalizeClientRequestId(
        current.resumeRequestId || providerHandoff?.clientRequestId,
      )
      storage.setItem(metaSetupManualStorageKey(current.sessionKey), JSON.stringify({
        name: current.name,
        launchText: normalizeMetaLaunchText(current.name, current.launchText),
        readiness: current.readiness,
        ...(providerHandoff ? { providerHandoff } : {}),
        ...(resumeRequestId ? { resumeRequestId } : {}),
        ...(current.suppressAutoResume ? { suppressAutoResume: true } : {}),
      }))
      return true
    } catch {
      // The card still works for the current route when sessionStorage is unavailable.
      return false
    }
  }

  function readPersistedSetupCheckpoint(sessionKey: string): MetaSetupState | null {
    if (!storage || !sessionKey) return null
    try {
      const raw = storage.getItem(metaSetupManualStorageKey(sessionKey))
      if (!raw) return null
      const parsed = JSON.parse(raw) as {
        name?: unknown
        launchText?: unknown
        readiness?: unknown
        providerHandoff?: unknown
        resumeRequestId?: unknown
        suppressAutoResume?: unknown
      }
      if (
        typeof parsed.name !== 'string'
        || !parsed.name
        || typeof parsed.readiness !== 'object'
        || parsed.readiness === null
        || Array.isArray(parsed.readiness)
      ) {
        clearPersistedManualSetup(sessionKey)
        return null
      }
      const restored = confirmState(
        parsed.name,
        parsed.readiness as MetaSetupReadiness,
        sessionKey,
        typeof parsed.launchText === 'string' ? parsed.launchText : `/meta ${parsed.name}`,
      )
      const providerHandoff = validProviderHandoff(
        parsed.providerHandoff,
        restored.readiness,
      )
      const resumeRequestId = normalizeClientRequestId(parsed.resumeRequestId)
      const providerMatchesResume = Boolean(
        providerHandoff
        && (!resumeRequestId || providerHandoff.clientRequestId === resumeRequestId),
      )
      if (providerHandoff && providerMatchesResume) {
        restored.providerHandoff = providerHandoff
        restored.resumeRequestId = resumeRequestId || providerHandoff.clientRequestId
      } else if (resumeRequestId) {
        restored.resumeRequestId = resumeRequestId
        restored.suppressAutoResume = Boolean(
          parsed.suppressAutoResume === true || parsed.providerHandoff !== undefined,
        )
      }
      if (
        (parsed.providerHandoff !== undefined && !providerMatchesResume)
        || (
          parsed.suppressAutoResume !== undefined
          && parsed.suppressAutoResume !== restored.suppressAutoResume
        )
        || (parsed.resumeRequestId !== undefined && !resumeRequestId)
      ) {
        persistSetupCheckpoint(restored)
      }
      return restored
    } catch {
      clearPersistedManualSetup(sessionKey)
      return null
    }
  }

  function readLegacySetupCheckpoint(sessionKey: string): MetaSetupState | null {
    const launchText = readPersistedLaunch(sessionKey)
    const match = /^\/meta\s+([^\s]+)(?:\s|$)/.exec(launchText)
    const name = match?.[1] || ''
    if (!name) return null
    const checkpoint = confirmState(name, {}, sessionKey, launchText)
    persistSetupCheckpoint(checkpoint)
    return checkpoint
  }

  function recoverFromMissingJob(
    sessionKey: string,
    fallback?: MetaSetupState,
  ): MetaSetupState | null {
    // The Gateway owns setup jobs in memory, so a restart legitimately makes
    // a previously accepted id unknown. Drop only that short-lived pointer;
    // the stable checkpoint remains the source of truth for user recovery.
    clearPersistedJobMarker(sessionKey)
    const persisted = readPersistedSetupCheckpoint(sessionKey)
    if (persisted) {
      const fallbackRequestId = setupResumeRequestId(fallback)
      const persistedRequestId = setupResumeRequestId(persisted)
      if (
        fallback
        && fallbackRequestId
        && persisted.name === fallback.name
        && normalizeMetaLaunchText(persisted.name, persisted.launchText)
          === normalizeMetaLaunchText(fallback.name, fallback.launchText)
        && (!persistedRequestId || persistedRequestId === fallbackRequestId)
      ) {
        const merged = {
          ...persisted,
          resumeRequestId: fallbackRequestId,
          providerHandoff: fallback.providerHandoff || persisted.providerHandoff,
        }
        persistSetupCheckpoint(merged)
        return merged
      }
      return persisted
    }

    if (fallback && fallback.name && fallback.name !== 'MetaSkill') {
      const checkpoint = confirmState(
        fallback.name,
        fallback.readiness,
        sessionKey,
        fallback.launchText || readPersistedLaunch(sessionKey),
      )
      checkpoint.resumeRequestId = fallback.resumeRequestId
      checkpoint.providerHandoff = fallback.providerHandoff
      persistSetupCheckpoint(checkpoint)
      return checkpoint
    }

    // Older clients persisted only a job id plus launch text. Preserve that
    // upgrade path as a readiness-recheck card when the old job disappeared.
    return readLegacySetupCheckpoint(sessionKey)
  }

  function failedState(
    current: MetaSetupState,
    error: string,
    retryMode: 'install' | 'status' | 'launch' | 'readiness' | 'discard',
  ): MetaSetupState {
    return {
      ...current,
      phase: 'failed',
      error,
      retryMode,
    }
  }

  function setupResumeRequestId(current: MetaSetupState | null | undefined): string {
    return normalizeClientRequestId(
      current?.resumeRequestId || current?.providerHandoff?.clientRequestId,
    )
  }

  function dispatchFailureMessage(result: HiddenControlDispatchResult): string {
    if (result.reason === 'queue_full') {
      return 'The pending queue is full. The MetaSkill request is still saved; retry when a slot is available.'
    }
    if (result.reason === 'discarded') {
      return 'The queued launch was removed. The MetaSkill request is still saved and can be retried.'
    }
    if (result.status === 'unknown') {
      return 'OpenSquilla could not confirm whether the launch was accepted. Retry safely with the same request identity.'
    }
    return 'The MetaSkill launch was not accepted. The original request is still saved and can be retried.'
  }

  /**
   * Complete a durable provider/setup resume only when the hidden chat ingress
   * reports that the Gateway accepted it. Queueing and ambiguous failures keep
   * the exact persisted request id so remounts and retries remain idempotent.
   */
  function handleHiddenDispatchResult(result: HiddenControlDispatchResult): void {
    const sessionKey = String(result.sessionKey || '')
    const clientRequestId = normalizeClientRequestId(result.clientRequestId)
    if (!sessionKey || !clientRequestId) return

    const current = setupState.value
    const currentMatches = Boolean(
      current
      && current.sessionKey === sessionKey
      && setupResumeRequestId(current) === clientRequestId,
    )
    const persisted = currentMatches ? null : readPersistedSetupCheckpoint(sessionKey)
    const persistedMatches = setupResumeRequestId(persisted) === clientRequestId
    if (!currentMatches && !persistedMatches) return

    if (result.status === 'accepted') {
      clearPersistedJobMarker(sessionKey)
      clearPersistedLaunch(sessionKey)
      clearPersistedManualSetup(sessionKey)
      if (currentMatches) {
        beginOperation()
        installInFlight = false
        setupState.value = null
      }
      return
    }

    if (!currentMatches || !current) return
    if (result.status === 'queued') {
      setupState.value = {
        ...current,
        phase: 'verifying',
        message: '',
        error: '',
        retryMode: undefined,
      }
      return
    }
    setupState.value = failedState(current, dispatchFailureMessage(result), 'launch')
  }

  function restoreFailedState(
    sessionKey: string,
    jobId: string,
    error: string,
    fallback?: MetaSetupState,
  ): MetaSetupState {
    return {
      name: fallback?.name || 'MetaSkill',
      sessionKey,
      launchText: fallback?.launchText || readPersistedLaunch(sessionKey),
      phase: 'failed',
      readiness: fallback?.readiness || {},
      actionIds: fallback?.actionIds || [],
      jobId,
      jobStatus: fallback?.jobStatus,
      message: fallback?.message || '',
      currentAction: '',
      downloadedBytes: fallback?.downloadedBytes || 0,
      downloadTotalBytes: fallback?.downloadTotalBytes || 0,
      completedActions: fallback?.completedActions || [],
      error,
      retryMode: 'status',
      providerHandoff: fallback?.providerHandoff,
      resumeRequestId: fallback?.resumeRequestId,
    }
  }

  function schedulePoll(jobId: string, sessionKey: string, token: number): void {
    if (!isCurrent(token)) return
    stopPolling()
    pollTimer = setTimeout(() => {
      pollTimer = null
      void pollJob(jobId, sessionKey, token)
    }, pollIntervalMs)
  }

  async function resumeAfterSetup(
    name: string,
    sessionKey: string,
    readiness: MetaSetupReadiness,
    token: number,
    clientRequestId = '',
  ): Promise<void> {
    if (!isCurrent(token)) return
    const current = setupState.value
    if (!current || current.name !== name || current.sessionKey !== sessionKey) return
    const stableClientRequestId = normalizeClientRequestId(
      clientRequestId || setupResumeRequestId(current),
    ) || createClientRequestId()
    const identifiedCurrent: MetaSetupState = {
      ...current,
      resumeRequestId: stableClientRequestId,
    }
    // A provider handoff is already durably stored against the pre-setup
    // readiness snapshot. Do not overwrite it with the post-check ready snapshot
    // (which intentionally no longer lists a manual provider action).
    persistSetupCheckpoint(identifiedCurrent)

    stopPolling()
    setupState.value = {
      ...identifiedCurrent,
      phase: 'verifying',
      readiness,
      message: '',
      error: '',
      retryMode: undefined,
    }

    if (options.currentSessionKey.value !== sessionKey) {
      setupState.value = {
        ...setupState.value,
        phase: 'blocked',
        blockedReason: 'session_changed',
      }
      return
    }

    try {
      const result = await rpcCall<MetaSetupRunResponse>('meta.run', {
        name,
        sessionKey,
        clientRequestId: stableClientRequestId,
        launchText: normalizeMetaLaunchText(name, current.launchText),
      })
      if (!isCurrent(token)) return

      if (options.currentSessionKey.value !== sessionKey) {
        setupState.value = {
          ...setupState.value!,
          phase: 'blocked',
          blockedReason: 'session_changed',
        }
        return
      }

      if (result?.ok) {
        const launchText = normalizeMetaLaunchText(name, setupState.value?.launchText)
        try {
          const dispatchResult = await options.dispatchHidden(
            launchText,
            launchText,
            stableClientRequestId,
          )
          handleHiddenDispatchResult(dispatchResult)
        } catch {
          handleHiddenDispatchResult({
            status: 'unknown',
            reason: 'response_unknown',
            clientRequestId: stableClientRequestId,
            sessionKey,
          })
        }
        return
      }

      if (result?.setup_required) {
        const nextReadiness = result.readiness || readiness
        clearPersistedJobMarker(sessionKey)
        const next = confirmState(
          name,
          nextReadiness,
          sessionKey,
          setupState.value?.launchText || `/meta ${name}`,
        )
        setupState.value = {
          ...next,
          error: result.error || next.error || '',
          blockedReason: next.phase === 'blocked' ? 'requirements_remaining' : undefined,
          providerHandoff: current.providerHandoff,
          resumeRequestId: stableClientRequestId,
        }
        persistSetupCheckpoint(setupState.value!)
        return
      }

      setupState.value = failedState(
        setupState.value!,
        result?.error || 'MetaSkill could not start after setup',
        'launch',
      )
    } catch (error) {
      if (!isCurrent(token) || !setupState.value) return
      const rpcError = error as RpcClientError | undefined
      if (rpcError?.code === 'META_DRAFT_DISCARDED') {
        // A cancellation committed in another tab wins over this stale setup
        // checkpoint. Consume it terminally without restoring sendable text.
        removePendingMetaDiscard(sessionKey, stableClientRequestId, discardStorage)
        try {
          options.forgetHiddenControl?.(sessionKey, stableClientRequestId)
        } catch {
          // The server tombstone remains authoritative even if local cleanup fails.
        }
        beginOperation()
        installInFlight = false
        clearPersistedJobMarker(sessionKey)
        clearPersistedLaunch(sessionKey)
        clearPersistedManualSetup(sessionKey)
        setupState.value = null
        return
      }
      setupState.value = failedState(setupState.value, errorMessage(error), 'launch')
    }
  }

  async function applyJob(job: MetaSetupJob, token: number): Promise<void> {
    if (!isCurrent(token)) return
    const current = setupState.value
    if (!current || current.sessionKey !== job.sessionKey) return
    const sameSetup = current.name === job.name
    const restoredSetup = Boolean(current.jobId && current.jobId === job.job_id)
    if (!sameSetup && !restoredSetup) return

    const completedActions = [...(job.completed_actions || [])]
    const remainingActionIds = (job.action_ids || current.actionIds)
      .filter(actionId => !completedActions.includes(actionId))
    const readiness = job.readiness || current.readiness
    const launchText = normalizeMetaLaunchText(
      job.name,
      current.launchText || readPersistedLaunch(job.sessionKey),
    )
    // Keep the pre-handoff readiness record while a provider resume is pending:
    // the completed job's ready snapshot no longer contains the provider action
    // needed to validate that durable marker.
    if (!setupResumeRequestId(current)) {
      persistSetupCheckpoint({
        ...current,
        name: job.name,
        launchText,
        readiness,
      })
    }

    if (job.status === 'completed' || job.phase === 'completed') {
      setupState.value = {
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
      }
      await resumeAfterSetup(job.name, job.sessionKey, readiness, token)
      return
    }

    if (job.status === 'blocked' || job.phase === 'blocked') {
      clearPersistedJobMarker(job.sessionKey)
      const next = confirmState(job.name, readiness, job.sessionKey, launchText)
      setupState.value = {
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
        // The setup job may complete its automatic actions while a provider
        // requirement remains. Keep the original launch identity across that
        // transition so provider settings resumes the already-drafted request
        // instead of allocating a second, independently chargeable launch.
        providerHandoff: current.providerHandoff,
        resumeRequestId: setupResumeRequestId(current) || undefined,
      }
      persistSetupCheckpoint(setupState.value!)
      return
    }

    if (job.status === 'failed' || job.phase === 'failed') {
      setupState.value = {
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
      }
      return
    }

    const phase = job.phase === 'verifying' ? 'verifying' : 'installing'
    setupState.value = {
      ...current,
      name: job.name,
      launchText,
      phase,
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
    }
    persistJob(job.sessionKey, job.job_id)
    schedulePoll(job.job_id, job.sessionKey, token)
  }

  async function pollJob(jobId: string, sessionKey: string, token: number): Promise<void> {
    if (!isCurrent(token)) return
    try {
      const result = await rpcCall<MetaSetupStatusResponse>('meta.setup.status', {
        jobId,
        sessionKey,
      })
      if (!isCurrent(token)) return
      if (!result?.job) throw new Error(result?.error || 'Setup status is unavailable')
      await applyJob(result.job, token)
    } catch (error) {
      if (!isCurrent(token) || !setupState.value) return
      if (isMissingJobError(error)) {
        setupState.value = recoverFromMissingJob(sessionKey, setupState.value)
        return
      }
      setupState.value = failedState(setupState.value, errorMessage(error), 'status')
    }
  }

  async function startInstall(current: MetaSetupState): Promise<void> {
    if (installInFlight || current.actionIds.length === 0) return
    if (options.currentSessionKey.value !== current.sessionKey) {
      setupState.value = {
        ...current,
        phase: 'blocked',
        blockedReason: 'session_changed',
      }
      return
    }

    const token = beginOperation()
    installInFlight = true
    persistSetupCheckpoint(current)
    setupState.value = {
      ...current,
      phase: 'installing',
      message: '',
      error: '',
      retryMode: undefined,
    }
    try {
      const result = await rpcCall<MetaSetupInstallResponse>('meta.setup.install', {
        name: current.name,
        sessionKey: current.sessionKey,
        confirmed: true,
        action_ids: current.actionIds,
      })
      if (!isCurrent(token)) return

      if (result?.already_ready) {
        await resumeAfterSetup(
          current.name,
          current.sessionKey,
          result.readiness || current.readiness,
          token,
        )
        return
      }
      if (!result?.job) throw new Error(result?.error || 'Setup did not start')
      persistJob(current.sessionKey, result.job.job_id)
      persistLaunch(
        current.sessionKey,
        normalizeMetaLaunchText(current.name, current.launchText),
      )
      await applyJob(result.job, token)
    } catch (error) {
      if (!isCurrent(token) || !setupState.value) return
      setupState.value = failedState(setupState.value, errorMessage(error), 'install')
    } finally {
      if (token === operationToken) installInFlight = false
    }
  }

  async function requestSetup(
    name: string,
    readiness: MetaSetupReadiness,
    originatingSessionKey: string,
    launchText = `/meta ${name}`,
    clientRequestId = '',
  ): Promise<void | 'visible' | 'deferred'> {
    const next = confirmState(name, readiness, originatingSessionKey, launchText)
    const stableClientRequestId = normalizeClientRequestId(clientRequestId)
    if (stableClientRequestId) next.resumeRequestId = stableClientRequestId
    if (options.currentSessionKey.value !== originatingSessionKey) {
      // meta.run can finish after the user navigates away. Keep the exact
      // setup intent under its originating session so returning to that chat
      // restores the card instead of silently losing the cleared composer.
      persistSetupCheckpoint(next)
      return 'deferred' as const
    }

    const visibleSetup = setupState.value
    if (
      visibleSetup
      && visibleSetup.sessionKey === originatingSessionKey
      && isBusyPhase(visibleSetup)
    ) {
      // A second /meta request must not cancel polling for the setup already
      // running in this chat. Keeping the existing card visible makes the
      // active operation explicit to the user. The second request is already
      // server-owned under its stable id; never turn it into ordinary composer
      // text (which would allocate a second id). It remains recoverable from
      // the server outbox after the active setup finishes or this chat reloads.
      return 'deferred' as const
    }

    const token = beginOperation()

    const persistedJobId = readPersistedJob(originatingSessionKey)
    if (!persistedJobId) {
      setupState.value = next
      persistSetupCheckpoint(next)
      return
    }
    try {
      const result = await rpcCall<MetaSetupStatusResponse>('meta.setup.status', {
        jobId: persistedJobId,
        sessionKey: originatingSessionKey,
      })
      if (!isCurrent(token)) return
      if (!result?.job) {
        const unavailable = result?.error || 'Setup status is unavailable'
        if (!isMissingJobError(unavailable)) throw new Error(unavailable)
        setupState.value = recoverFromMissingJob(originatingSessionKey, next)
        return
      }
      const checkpoint = readPersistedSetupCheckpoint(originatingSessionKey)
      const persistedLaunch = readPersistedLaunch(originatingSessionKey)
      const incumbentLaunch = checkpoint?.launchText || persistedLaunch
      const incumbentRequestId = setupResumeRequestId(checkpoint)
      const incumbentCoordinatesDiffer = (
        result.job.name !== name
        || Boolean(
          incumbentLaunch
          && normalizeMetaLaunchText(result.job.name, incumbentLaunch)
            !== normalizeMetaLaunchText(name, next.launchText),
        )
      )
      // An accepted job belongs to the checkpoint that created it. A newer
      // durable request must remain a separate server-outbox entry rather than
      // borrowing that job and losing its own idempotency identity.
      if (
        stableClientRequestId
        && (
          incumbentCoordinatesDiffer
          || Boolean(incumbentRequestId && incumbentRequestId !== stableClientRequestId)
        )
      ) {
        setupState.value = checkpoint || confirmState(
          result.job.name,
          result.job.readiness || {},
          originatingSessionKey,
          incumbentLaunch || `/meta ${result.job.name}`,
        )
        await applyJob(result.job, token)
        return 'deferred' as const
      }
      const restoredReadiness = result.job.readiness || (result.job.name === name ? readiness : {})
      setupState.value = {
        ...(checkpoint || next),
        name: result.job.name,
        sessionKey: originatingSessionKey,
        launchText: normalizeMetaLaunchText(
          result.job.name,
          persistedLaunch,
        ),
        phase: result.job.phase === 'verifying' ? 'verifying' : 'installing',
        readiness: restoredReadiness,
        actionIds: result.job.action_ids || [],
        jobId: result.job.job_id,
        jobStatus: result.job.status,
        message: result.job.message || '',
        currentAction: result.job.current_action || '',
        downloadedBytes: result.job.downloaded_bytes || 0,
        downloadTotalBytes: result.job.download_total_bytes || 0,
        completedActions: result.job.completed_actions || [],
        providerHandoff: checkpoint?.providerHandoff || next.providerHandoff,
        resumeRequestId: checkpoint?.resumeRequestId || next.resumeRequestId,
      }
      if (setupState.value) persistSetupCheckpoint(setupState.value)
      await applyJob(result.job, token)
    } catch (error) {
      if (!isCurrent(token)) return
      if (isMissingJobError(error)) {
        setupState.value = recoverFromMissingJob(originatingSessionKey, next)
        return
      }
      setupState.value = restoreFailedState(
        originatingSessionKey,
        persistedJobId,
        errorMessage(error),
        next,
      )
    }
  }

  async function confirmSetup(): Promise<void> {
    const current = setupState.value
    if (!current || current.phase !== 'confirm') return
    await startInstall(current)
  }

  function beginProviderHandoff(providerId: string): string {
    const current = setupState.value
    const normalizedProviderId = normalizeProviderId(providerId)
    if (
      !current
      || isBusyPhase(current)
      || current.sessionKey !== options.currentSessionKey.value
      || !availableProviderIds(current.readiness).includes(normalizedProviderId)
      || Boolean(validProviderHandoff(current.providerHandoff, current.readiness))
    ) {
      return ''
    }

    const clientRequestId = setupResumeRequestId(current) || createClientRequestId()
    const { suppressAutoResume: _suppressAutoResume, ...resumableCurrent } = current
    const next: MetaSetupState = {
      ...resumableCurrent,
      resumeRequestId: clientRequestId,
      providerHandoff: {
        kind: 'provider_settings',
        providerId: normalizedProviderId,
        startedAtMs: Date.now(),
        clientRequestId,
      },
    }
    // A handoff necessarily unmounts ChatView. Do not leave the page unless its
    // original intent and idempotency identity are durably recoverable.
    if (!persistSetupCheckpoint(next)) return ''
    setupState.value = next
    return clientRequestId
  }

  function cancelProviderHandoff(providerId: string, clientRequestId: string): void {
    const current = setupState.value
    if (!current?.providerHandoff) return
    const normalizedProviderId = normalizeProviderId(providerId)
    const normalizedRequestId = normalizeClientRequestId(clientRequestId)
    if (
      !normalizedProviderId
      || !normalizedRequestId
      || normalizedProviderId !== current.providerHandoff.providerId
      || normalizedRequestId !== current.providerHandoff.clientRequestId
    ) return

    // The handoff marker is one-shot navigation state. The resume identity is
    // the durable server draft identity and must survive a cancelled/failed
    // route so a later dismissal can atomically discard that exact request.
    const { providerHandoff: _providerHandoff, ...next } = current
    setupState.value = next
    persistSetupCheckpoint(next)
  }

  function finishAlreadyAcceptedDraft(
    current: MetaSetupState,
    clientRequestId: string,
  ): void {
    removePendingMetaDiscard(current.sessionKey, clientRequestId, discardStorage)
    beginOperation()
    installInFlight = false
    clearPersistedJobMarker(current.sessionKey)
    clearPersistedLaunch(current.sessionKey)
    clearPersistedManualSetup(current.sessionKey)
    setupState.value = null
    try {
      options.onDraftAlreadyAccepted?.(current.sessionKey, clientRequestId)
    } catch {
      // Notification failures must not resurrect an accepted paid launch.
    }
  }

  async function recheckReadiness(
    current: MetaSetupState,
    clientRequestId = '',
  ): Promise<void> {
    if (options.currentSessionKey.value !== current.sessionKey) {
      setupState.value = {
        ...current,
        phase: 'blocked',
        blockedReason: 'session_changed',
        retryMode: undefined,
      }
      return
    }

    const token = beginOperation()
    setupState.value = {
      ...current,
      suppressAutoResume: undefined,
      phase: 'verifying',
      message: '',
      error: '',
      retryMode: undefined,
    }
    const stableClientRequestId = normalizeClientRequestId(
      clientRequestId || setupResumeRequestId(current),
    )
    if (stableClientRequestId) {
      // meta.run is itself the authoritative, idempotent readiness check for a
      // durable draft. Calling it first also observes a cancellation tombstone
      // immediately, even while provider requirements are still missing.
      await resumeAfterSetup(
        current.name,
        current.sessionKey,
        current.readiness,
        token,
        stableClientRequestId,
      )
      return
    }
    try {
      const result = await rpcCall<MetaSetupPlanResponse>('meta.setup.plan', {
        name: current.name,
      })
      if (!isCurrent(token) || !setupState.value) return
      if (options.currentSessionKey.value !== current.sessionKey) return
      if (!result?.ok || !result.readiness) {
        throw new Error(result?.error || 'MetaSkill readiness could not be checked')
      }

      if (result.readiness.ready) {
        await resumeAfterSetup(
          current.name,
          current.sessionKey,
          result.readiness,
          token,
          clientRequestId || setupResumeRequestId(current),
        )
        return
      }

      setupState.value = confirmState(
        current.name,
        result.readiness,
        current.sessionKey,
        current.launchText || `/meta ${current.name}`,
      )
      if (setupState.value && stableClientRequestId) {
        setupState.value.resumeRequestId = stableClientRequestId
      }
      if (setupState.value) persistSetupCheckpoint(setupState.value)
    } catch (error) {
      if (!isCurrent(token) || !setupState.value) return
      setupState.value = failedState(setupState.value, errorMessage(error), 'readiness')
    }
  }

  async function retrySetup(): Promise<void> {
    const current = setupState.value
    if (!current || current.phase === 'installing' || current.phase === 'verifying') return
    if (current.retryMode === 'discard') {
      await retrySetupDiscard(current)
      return
    }
    if (current.retryMode === 'status' && current.jobId) {
      const token = beginOperation()
      setupState.value = { ...current, phase: 'verifying', error: '' }
      await pollJob(current.jobId, current.sessionKey, token)
      return
    }
    if (current.retryMode === 'launch') {
      const token = beginOperation()
      await resumeAfterSetup(
        current.name,
        current.sessionKey,
        current.readiness,
        token,
        setupResumeRequestId(current),
      )
      return
    }
    if (current.retryMode === 'readiness') {
      await recheckReadiness(current, setupResumeRequestId(current))
      return
    }
    if (current.actionIds.length) await startInstall(current)
  }

  async function retrySetupDiscard(current: MetaSetupState): Promise<void> {
    const clientRequestId = setupResumeRequestId(current)
    const launchText = current.name === 'MetaSkill'
      ? ''
      : normalizeMetaLaunchText(current.name, current.launchText)
    if (!clientRequestId) return
    cancelInFlight = true
    try {
      const outcome = normalizeDiscardOutcome(
        await options.discardDraft?.(current.sessionKey, clientRequestId),
      )
      if (outcome === 'accepted') {
        finishAlreadyAcceptedDraft(current, clientRequestId)
        return
      }
      if (outcome !== 'discarded') {
        setupState.value = failedState(
          current,
          'The saved cancellation is still pending. The request will not be launched.',
          'discard',
        )
        persistSetupCheckpoint(setupState.value)
        return
      }
      removePendingMetaDiscard(current.sessionKey, clientRequestId, discardStorage)
      beginOperation()
      installInFlight = false
      clearPersistedJobMarker(current.sessionKey)
      clearPersistedLaunch(current.sessionKey)
      clearPersistedManualSetup(current.sessionKey)
      setupState.value = null
      if (launchText) options.restoreDraft?.(launchText, current.sessionKey)
    } catch (error) {
      setupState.value = failedState(
        current,
        `The saved cancellation could not be completed: ${errorMessage(error)}`,
        'discard',
      )
      persistSetupCheckpoint(setupState.value)
    } finally {
      cancelInFlight = false
    }
  }

  async function cancelSetup(): Promise<void> {
    const current = setupState.value
    if (!current || cancelInFlight) return
    const draftToRestore = !isBusyPhase(current)
      ? normalizeMetaLaunchText(current.name, current.launchText)
      : ''
    const clientRequestId = setupResumeRequestId(current)

    if (draftToRestore && clientRequestId) {
      if (!persistPendingMetaDiscard({
        sessionKey: current.sessionKey,
        clientRequestId,
      }, discardStorage)) {
        setupState.value = failedState(
          current,
          'The cancellation could not be saved safely. The request was not launched.',
          'discard',
        )
        return
      }
      cancelInFlight = true
      try {
        const outcome = normalizeDiscardOutcome(
          await options.discardDraft?.(current.sessionKey, clientRequestId),
        )
        if (outcome === 'accepted') {
          finishAlreadyAcceptedDraft(current, clientRequestId)
          return
        }
        if (outcome !== 'discarded') {
          setupState.value = failedState(
            current,
            'The saved request could not be discarded. It remains pending with the same request identity.',
            'discard',
          )
          persistSetupCheckpoint(setupState.value)
          return
        }
      } catch (error) {
        setupState.value = failedState(
          current,
          `The saved request could not be discarded: ${errorMessage(error)}`,
          'discard',
        )
        persistSetupCheckpoint(setupState.value)
        return
      } finally {
        cancelInFlight = false
      }
      removePendingMetaDiscard(current.sessionKey, clientRequestId, discardStorage)
    }

    beginOperation()
    installInFlight = false
    if (current.phase !== 'installing' && current.phase !== 'verifying') {
      clearPersistedJobMarker(current.sessionKey)
      clearPersistedLaunch(current.sessionKey)
    }
    clearPersistedManualSetup(current.sessionKey)
    setupState.value = null
    if (draftToRestore) {
      options.restoreDraft?.(draftToRestore, current.sessionKey)
    }
  }

  async function restoreSetupJob(sessionKey = options.currentSessionKey.value): Promise<void> {
    if (!sessionKey || sessionKey !== options.currentSessionKey.value) return
    const checkpoint = readPersistedSetupCheckpoint(sessionKey)
    const checkpointRequestId = setupResumeRequestId(checkpoint)
    const pendingDiscards = listPendingMetaDiscards(sessionKey, discardStorage)
    const pendingDiscard = checkpointRequestId
      ? pendingDiscards.find(item => item.clientRequestId === checkpointRequestId)
      : undefined
    if (pendingDiscard) {
      const fallback = checkpoint || confirmState(
        'MetaSkill',
        {},
        sessionKey,
        '/meta MetaSkill',
      )
      setupState.value = {
        ...fallback,
        phase: 'failed',
        retryMode: 'discard',
        resumeRequestId: pendingDiscard.clientRequestId,
        error: 'Finishing cancellation of the saved MetaSkill request.',
      }
      await retrySetupDiscard(setupState.value)
      return
    }
    const jobId = readPersistedJob(sessionKey)
    if (!jobId) {
      const restored = readPersistedSetupCheckpoint(sessionKey)
      setupState.value = restored
      const resumeRequestId = setupResumeRequestId(restored)
      if (restored && resumeRequestId && !restored.suppressAutoResume) {
        // Retain the checkpoint until chat ingress explicitly reports durable
        // acceptance. Re-running this path is safe because the stable request id
        // is also the Gateway idempotency key.
        await recheckReadiness(restored, resumeRequestId)
      }
      return
    }
    const token = beginOperation()
    try {
      const result = await rpcCall<MetaSetupStatusResponse>('meta.setup.status', {
        jobId,
        sessionKey,
      })
      if (!isCurrent(token) || sessionKey !== options.currentSessionKey.value) return
      if (!result?.job) {
        const unavailable = result?.error || 'Setup status is unavailable'
        if (!isMissingJobError(unavailable)) throw new Error(unavailable)
        setupState.value = recoverFromMissingJob(sessionKey)
        return
      }
      const readiness = result.job.readiness || {}
      setupState.value = {
        ...(checkpoint || {}),
        name: result.job.name,
        sessionKey,
        launchText: normalizeMetaLaunchText(
          result.job.name,
          readPersistedLaunch(sessionKey),
        ),
        phase: result.job.phase === 'verifying' ? 'verifying' : 'installing',
        readiness,
        actionIds: result.job.action_ids || [],
        jobId,
        jobStatus: result.job.status,
        message: result.job.message || '',
        currentAction: result.job.current_action || '',
        downloadedBytes: result.job.downloaded_bytes || 0,
        downloadTotalBytes: result.job.download_total_bytes || 0,
        completedActions: result.job.completed_actions || [],
        providerHandoff: checkpoint?.providerHandoff,
        resumeRequestId: checkpoint?.resumeRequestId,
      }
      await applyJob(result.job, token)
    } catch (error) {
      if (!isCurrent(token)) return
      if (isMissingJobError(error)) {
        setupState.value = recoverFromMissingJob(sessionKey)
        return
      }
      setupState.value = restoreFailedState(
        sessionKey,
        jobId,
        errorMessage(error),
        checkpoint || undefined,
      )
    }
  }

  const stopSessionWatch = watch(options.currentSessionKey, (nextSessionKey) => {
    const current = setupState.value
    if (current && current.sessionKey !== nextSessionKey) {
      beginOperation()
      installInFlight = false
      setupState.value = null
    }
    if (options.autoRestore !== false) void restoreSetupJob(nextSessionKey)
  })

  function dispose(): void {
    if (disposed) return
    disposed = true
    operationToken += 1
    stopPolling()
    stopSessionWatch()
  }

  if (getCurrentScope()) onScopeDispose(dispose)
  if (options.autoRestore !== false) void restoreSetupJob()

  return {
    setupState,
    requestSetup,
    confirmSetup,
    beginProviderHandoff,
    cancelProviderHandoff,
    retrySetup,
    cancelSetup,
    restoreSetupJob,
    handleHiddenDispatchResult,
    dispose,
  }
}
