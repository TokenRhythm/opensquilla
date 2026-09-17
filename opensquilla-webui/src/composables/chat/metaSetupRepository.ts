import type { MetaSetupReadiness, MetaSetupState } from '@/types/metaSetup'

import {
  createMetaSetupConfirmationState,
  metaSetupResumeRequestId,
  normalizeMetaLaunchText,
  normalizeMetaSetupClientRequestId,
  validMetaSetupProviderHandoff,
} from './metaSetupMachine'

// These key names and checkpoint fields are an upgrade contract. The
// repository deliberately persists recovery inputs, never live job progress.
const STORAGE_PREFIX = 'opensquilla.chat.metaSetupJob:'
const LAUNCH_STORAGE_PREFIX = 'opensquilla.chat.metaSetupLaunch:'
const MANUAL_STORAGE_PREFIX = 'opensquilla.chat.metaSetupManual:'

export interface MetaSetupStorage {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
  removeItem: (key: string) => void
}

export function metaSetupStorageKey(sessionKey: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(sessionKey)}`
}

export function metaSetupLaunchStorageKey(sessionKey: string): string {
  return `${LAUNCH_STORAGE_PREFIX}${encodeURIComponent(sessionKey)}`
}

export function metaSetupManualStorageKey(sessionKey: string): string {
  return `${MANUAL_STORAGE_PREFIX}${encodeURIComponent(sessionKey)}`
}

export function defaultMetaSetupStorage(): MetaSetupStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

export function defaultMetaSetupDiscardStorage(): MetaSetupStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export interface MetaSetupRepository {
  readJob: (sessionKey: string) => string
  persistJob: (sessionKey: string, jobId: string) => void
  readLaunch: (sessionKey: string) => string
  persistLaunch: (sessionKey: string, launchText: string) => void
  clearJob: (sessionKey: string) => void
  clearLaunch: (sessionKey: string) => void
  clearCheckpoint: (sessionKey: string) => void
  persistCheckpoint: (current: MetaSetupState) => boolean
  readCheckpoint: (sessionKey: string) => MetaSetupState | null
  recoverFromMissingJob: (
    sessionKey: string,
    fallback?: MetaSetupState,
  ) => MetaSetupState | null
}

export function createMetaSetupRepository(
  storage: MetaSetupStorage | null,
): MetaSetupRepository {
  function readJob(sessionKey: string): string {
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

  function readLaunch(sessionKey: string): string {
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

  function clearJob(sessionKey: string): void {
    if (!storage || !sessionKey) return
    try {
      storage.removeItem(metaSetupStorageKey(sessionKey))
    } catch {
      // Best-effort cleanup only.
    }
  }

  function clearLaunch(sessionKey: string): void {
    if (!storage || !sessionKey) return
    try {
      storage.removeItem(metaSetupLaunchStorageKey(sessionKey))
    } catch {
      // Best-effort cleanup only.
    }
  }

  function clearCheckpoint(sessionKey: string): void {
    if (!storage || !sessionKey) return
    try {
      storage.removeItem(metaSetupManualStorageKey(sessionKey))
    } catch {
      // Best-effort cleanup only.
    }
  }

  function persistCheckpoint(current: MetaSetupState): boolean {
    // Persist only stable recovery inputs, never server-owned progress. Keeping
    // this checkpoint while a job is active lets the UI recover the original
    // readiness and launch after a Gateway restart invalidates the job id.
    if (!storage || !current.sessionKey) return false
    try {
      const providerHandoff = validMetaSetupProviderHandoff(
        current.providerHandoff,
        current.readiness,
      )
      const resumeRequestId = normalizeMetaSetupClientRequestId(
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

  function readCheckpoint(sessionKey: string): MetaSetupState | null {
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
        clearCheckpoint(sessionKey)
        return null
      }
      const restored = createMetaSetupConfirmationState(
        parsed.name,
        parsed.readiness as MetaSetupReadiness,
        sessionKey,
        typeof parsed.launchText === 'string' ? parsed.launchText : `/meta ${parsed.name}`,
      )
      const providerHandoff = validMetaSetupProviderHandoff(
        parsed.providerHandoff,
        restored.readiness,
      )
      const resumeRequestId = normalizeMetaSetupClientRequestId(parsed.resumeRequestId)
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
        persistCheckpoint(restored)
      }
      return restored
    } catch {
      clearCheckpoint(sessionKey)
      return null
    }
  }

  function readLegacyCheckpoint(sessionKey: string): MetaSetupState | null {
    const launchText = readLaunch(sessionKey)
    const match = /^\/meta\s+([^\s]+)(?:\s|$)/.exec(launchText)
    const name = match?.[1] || ''
    if (!name) return null
    const checkpoint = createMetaSetupConfirmationState(name, {}, sessionKey, launchText)
    persistCheckpoint(checkpoint)
    return checkpoint
  }

  function recoverFromMissingJob(
    sessionKey: string,
    fallback?: MetaSetupState,
  ): MetaSetupState | null {
    // Gateway setup jobs are process-local. Keep the stable checkpoint when a
    // restart makes the short-lived job pointer unknown.
    clearJob(sessionKey)
    const persisted = readCheckpoint(sessionKey)
    if (persisted) {
      const fallbackRequestId = metaSetupResumeRequestId(fallback)
      const persistedRequestId = metaSetupResumeRequestId(persisted)
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
        persistCheckpoint(merged)
        return merged
      }
      return persisted
    }

    if (fallback && fallback.name && fallback.name !== 'MetaSkill') {
      const checkpoint = createMetaSetupConfirmationState(
        fallback.name,
        fallback.readiness,
        sessionKey,
        fallback.launchText || readLaunch(sessionKey),
      )
      checkpoint.resumeRequestId = fallback.resumeRequestId
      checkpoint.providerHandoff = fallback.providerHandoff
      persistCheckpoint(checkpoint)
      return checkpoint
    }

    // Older clients persisted only a job id plus launch text. Preserve that
    // upgrade path as a readiness-recheck card when the old job disappeared.
    return readLegacyCheckpoint(sessionKey)
  }

  return {
    readJob,
    persistJob,
    readLaunch,
    persistLaunch,
    clearJob,
    clearLaunch,
    clearCheckpoint,
    persistCheckpoint,
    readCheckpoint,
    recoverFromMissingJob,
  }
}
