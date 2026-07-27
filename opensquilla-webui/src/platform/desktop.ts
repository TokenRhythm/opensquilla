import { desktopCapabilities } from './capabilities'
import type {
  CliInvocation,
  DesktopUpdateErrorCode,
  DesktopUpdateInstallMode,
  DesktopUpdateSource,
  DesktopUpdateState,
  DesktopUpdateStatus,
  Platform,
} from './types'

function requireDesktopApi(): OpenSquillaDesktopApi {
  const api = window.opensquillaDesktop
  if (!api) throw new Error('OpenSquilla desktop API is unavailable.')
  return api
}

const UPDATE_STATUSES = new Set<DesktopUpdateStatus>([
  'idle',
  'checking',
  'available',
  'downloading',
  'downloaded',
  'not-available',
  'error',
  'applying',
])

const UPDATE_INSTALL_MODES = new Set<DesktopUpdateInstallMode>(['native', 'manual', 'unsupported'])
const UPDATE_ERROR_CODES = new Set<DesktopUpdateErrorCode>([
  'source_unreachable',
  'manifest_invalid',
  'checksum_unavailable',
  'integrity_failed',
  'download_failed',
  'install_failed',
])
const UPDATE_SOURCES = new Set<DesktopUpdateSource>(['oss', 'github'])

function idleUpdateState(canNativeInstall: boolean, managed = canNativeInstall): DesktopUpdateState {
  return {
    status: 'idle',
    currentVersion: '',
    latestVersion: null,
    progress: null,
    checkedAt: null,
    error: null,
    errorCode: null,
    snoozedUntil: null,
    canCheck: managed,
    canNativeInstall,
    installMode: canNativeInstall ? 'native' : managed ? 'manual' : 'unsupported',
    releaseUrl: null,
    source: null,
    fallbackUsed: false,
  }
}

function normalizeUpdateState(
  payload: unknown,
  canNativeInstall: boolean,
  managed = canNativeInstall,
): DesktopUpdateState {
  const raw = payload && typeof payload === 'object'
    ? payload as Partial<Record<keyof DesktopUpdateState, unknown>>
    : {}
  const status = String(raw.status || '')
  const progress = typeof raw.progress === 'number' && Number.isFinite(raw.progress)
    ? Math.max(0, Math.min(100, raw.progress))
    : null
  const normalizedNativeInstall = typeof raw.canNativeInstall === 'boolean'
    ? raw.canNativeInstall
    : canNativeInstall
  const normalizedCanCheck = typeof raw.canCheck === 'boolean' ? raw.canCheck : managed
  const installMode = String(raw.installMode || '')
  const errorCode = String(raw.errorCode || '')
  const source = String(raw.source || '')
  return {
    status: UPDATE_STATUSES.has(status as DesktopUpdateStatus) ? status as DesktopUpdateStatus : 'idle',
    currentVersion: typeof raw.currentVersion === 'string' ? raw.currentVersion : '',
    latestVersion: typeof raw.latestVersion === 'string' && raw.latestVersion ? raw.latestVersion : null,
    progress,
    checkedAt: typeof raw.checkedAt === 'string' && raw.checkedAt ? raw.checkedAt : null,
    error: typeof raw.error === 'string' && raw.error ? raw.error : null,
    errorCode: UPDATE_ERROR_CODES.has(errorCode as DesktopUpdateErrorCode)
      ? errorCode as DesktopUpdateErrorCode
      : null,
    snoozedUntil: typeof raw.snoozedUntil === 'string' && raw.snoozedUntil ? raw.snoozedUntil : null,
    canCheck: normalizedCanCheck,
    canNativeInstall: normalizedNativeInstall,
    installMode: UPDATE_INSTALL_MODES.has(installMode as DesktopUpdateInstallMode)
      ? installMode as DesktopUpdateInstallMode
      : normalizedNativeInstall ? 'native' : normalizedCanCheck ? 'manual' : 'unsupported',
    releaseUrl: typeof raw.releaseUrl === 'string' && raw.releaseUrl ? raw.releaseUrl : null,
    source: UPDATE_SOURCES.has(source as DesktopUpdateSource) ? source as DesktopUpdateSource : null,
    fallbackUsed: raw.fallbackUsed === true,
  }
}

async function nativeUpdateCapability(api: OpenSquillaDesktopApi): Promise<boolean> {
  if (typeof api.isAutoUpdateEnabled !== 'function') return true
  try {
    return await api.isAutoUpdateEnabled()
  } catch {
    return true
  }
}

async function managedUpdateCapability(
  api: OpenSquillaDesktopApi,
  nativeCapability?: boolean,
): Promise<boolean> {
  if (typeof api.isDesktopUpdateManaged !== 'function') {
    return nativeCapability ?? nativeUpdateCapability(api)
  }
  try {
    return await api.isDesktopUpdateManaged()
  } catch {
    // A shell exposing the managed bridge owns the update surface. Fail closed
    // against a duplicate gateway banner if that capability query is transiently
    // unavailable.
    return true
  }
}

async function desktopUpdateCapabilities(api: OpenSquillaDesktopApi): Promise<{
  canNativeInstall: boolean
  managed: boolean
}> {
  const canNativeInstall = await nativeUpdateCapability(api)
  return {
    canNativeInstall,
    managed: await managedUpdateCapability(api, canNativeInstall),
  }
}

async function desktopUpdateFallbackState(api: OpenSquillaDesktopApi): Promise<DesktopUpdateState> {
  const capabilities = await desktopUpdateCapabilities(api)
  return idleUpdateState(capabilities.canNativeInstall, capabilities.managed)
}

async function normalizeDesktopUpdatePayload(
  api: OpenSquillaDesktopApi,
  payload: unknown,
): Promise<DesktopUpdateState> {
  if (payload && typeof payload === 'object') {
    const raw = payload as { canCheck?: unknown; canNativeInstall?: unknown }
    if (typeof raw.canCheck === 'boolean' && typeof raw.canNativeInstall === 'boolean') {
      return normalizeUpdateState(payload, raw.canNativeInstall, raw.canCheck)
    }
  }
  const capabilities = await desktopUpdateCapabilities(api)
  return normalizeUpdateState(payload, capabilities.canNativeInstall, capabilities.managed)
}

export function createDesktopPlatform(): Platform {
  return {
    id: 'desktop',
    capabilities: desktopCapabilities,
    getOsLocale: () => requireDesktopApi().getOsLocale(),
    async setNativeTheme(payload) {
      const api = requireDesktopApi()
      if (typeof api.setNativeTheme !== 'function') return undefined
      return api.setNativeTheme(payload)
    },
    async nativeAutoUpdateEnabled() {
      const api = requireDesktopApi()
      // Older shells without this bridge are macOS-only with native update on;
      // default to true there so the web banner never double-notifies.
      return nativeUpdateCapability(api)
    },
    async desktopUpdateManaged() {
      const api = requireDesktopApi()
      return managedUpdateCapability(api)
    },
    gateway: {
      getStatus: () => requireDesktopApi().getGatewayStatus(),
      revealLog: () => requireDesktopApi().revealGatewayLog(),
      retryStartup: () => requireDesktopApi().retryStartup(),
      async getCliInvocation(): Promise<CliInvocation | null> {
        const api = requireDesktopApi()
        if (typeof api.getCliInvocation !== 'function') return null
        try {
          const raw = await api.getCliInvocation() as Partial<CliInvocation> | null
          if (!raw || typeof raw.prefix !== 'string' || !raw.prefix.trim()) return null
          return { mode: raw.mode === 'dev' ? 'dev' : 'bundled', prefix: raw.prefix }
        } catch {
          return null
        }
      },
    },
    settings: {
      getDesktopSettings: () => requireDesktopApi().getDesktopSettings(),
      saveDesktopSettings: (payload) => requireDesktopApi().saveDesktopSettings(payload),
      resetDesktopSettings: () => requireDesktopApi().resetDesktopSettings(),
      ...(typeof window.opensquillaDesktop?.getDesktopPreferences === 'function'
        && typeof window.opensquillaDesktop?.saveDesktopPreferences === 'function'
        ? {
            getDesktopPreferences: () => requireDesktopApi().getDesktopPreferences!(),
            saveDesktopPreferences: (payload) => (
              requireDesktopApi().saveDesktopPreferences!(payload)
            ),
          }
        : {}),
    },
    onboarding: {
      getDefaults: () => requireDesktopApi().getOnboardingDefaults(),
      save: (payload) => requireDesktopApi().saveOnboarding(payload),
      cancel: () => requireDesktopApi().cancelOnboarding(),
    },
    files: {
      openArtifact: (payload) => requireDesktopApi().openArtifact(payload),
    },
    updates: {
      async getState() {
        const api = requireDesktopApi()
        if (typeof api.getUpdateState !== 'function') return desktopUpdateFallbackState(api)
        return normalizeDesktopUpdatePayload(api, await api.getUpdateState())
      },
      async check() {
        const api = requireDesktopApi()
        if (typeof api.checkForUpdates !== 'function') return desktopUpdateFallbackState(api)
        return normalizeDesktopUpdatePayload(api, await api.checkForUpdates())
      },
      async download() {
        const api = requireDesktopApi()
        if (typeof api.downloadUpdate !== 'function') return desktopUpdateFallbackState(api)
        return normalizeDesktopUpdatePayload(api, await api.downloadUpdate())
      },
      async relaunch() {
        const api = requireDesktopApi()
        if (typeof api.relaunchToUpdate !== 'function') return desktopUpdateFallbackState(api)
        return normalizeDesktopUpdatePayload(api, await api.relaunchToUpdate())
      },
      async dismiss() {
        const api = requireDesktopApi()
        if (typeof api.dismissUpdate !== 'function') return desktopUpdateFallbackState(api)
        return normalizeDesktopUpdatePayload(api, await api.dismissUpdate())
      },
      onState(callback) {
        const api = requireDesktopApi()
        if (typeof api.onUpdateState !== 'function') return () => undefined
        return api.onUpdateState((payload) => {
          void normalizeDesktopUpdatePayload(api, payload).then(callback)
        })
      },
    },
  }
}
