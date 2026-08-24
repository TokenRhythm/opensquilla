import { desktopCapabilities } from './capabilities'
import type {
  CliInvocation,
  DesktopUpdateErrorCode,
  DesktopUpdateInstallMode,
  DesktopUpdateSource,
  DesktopUpdateState,
  DesktopUpdateStatus,
  NativeWorkbenchApi,
  NativeWorkbenchCapabilities,
  NativeWorkbenchSurfaceEvent,
  NativeWorkbenchSurfaceEventType,
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
const NATIVE_SURFACE_EVENT_TYPES = new Set<NativeWorkbenchSurfaceEventType>([
  'loading',
  'ready',
  'missing-resource',
  'navigation-state',
  'permission-request',
  'blocked-action',
  'capability-expired',
  'unresponsive',
  'responsive',
  'error',
  'crashed',
  'escape',
  'annotation-selected',
  'annotation-draft-change',
  'annotation-submit',
  'annotation-cancel',
  'annotation-overlay-fallback',
  'agent-edit-released',
])

function normalizeArtifactAnnotationSelection(
  value: unknown,
): import('./types').NativeArtifactAnnotationSelection | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const rect = raw.rect && typeof raw.rect === 'object'
    ? raw.rect as Record<string, unknown>
    : null
  const selectionId = typeof raw.selectionId === 'string' ? raw.selectionId : ''
  const tagName = typeof raw.tagName === 'string' ? raw.tagName : ''
  const elementPath = typeof raw.elementPath === 'string' ? raw.elementPath : ''
  const elementProofSha256 = typeof raw.elementProofSha256 === 'string'
    ? raw.elementProofSha256
    : ''
  const domSha256 = typeof raw.domSha256 === 'string' ? raw.domSha256 : ''
  const finite = (field: string) => typeof rect?.[field] === 'number'
    && Number.isFinite(rect[field])
  if (
    !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(selectionId)
    || !/^[A-Za-z][A-Za-z0-9:_-]{0,127}$/.test(tagName)
    || !elementPath
    || elementPath.length > 4096
    || !/^[a-f0-9]{64}$/i.test(elementProofSha256)
    || (domSha256 !== '' && !/^[a-f0-9]{64}$/i.test(domSha256))
    || !rect
    || !finite('x')
    || !finite('y')
    || !finite('width')
    || !finite('height')
  ) return null
  return {
    selectionId,
    tagName: tagName.toLowerCase(),
    elementPath,
    elementProofSha256: elementProofSha256.toLowerCase(),
    ...(domSha256 ? { domSha256: domSha256.toLowerCase() } : {}),
    rect: {
      x: Number(rect.x),
      y: Number(rect.y),
      width: Math.max(0, Number(rect.width)),
      height: Math.max(0, Number(rect.height)),
    },
  }
}

function normalizeNativeSurfaceEvent(payload: unknown): NativeWorkbenchSurfaceEvent | null {
  if (!payload || typeof payload !== 'object') return null
  const raw = payload as Record<string, unknown>
  if (
    (raw.version !== 1 && raw.version !== 2 && raw.version !== 3 && raw.version !== 4)
    || typeof raw.surfaceId !== 'string'
    || !NATIVE_SURFACE_EVENT_TYPES.has(raw.type as NativeWorkbenchSurfaceEventType)
  ) return null
  const annotationEvent = typeof raw.type === 'string' && raw.type.startsWith('annotation-')
  if (annotationEvent && raw.version !== 3 && raw.version !== 4) return null
  const rawDetail = raw.detail && typeof raw.detail === 'object'
    ? raw.detail as Record<string, unknown>
    : null
  const selection = normalizeArtifactAnnotationSelection(rawDetail?.selection)
  const annotationId = typeof rawDetail?.annotationId === 'string'
    && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(rawDetail.annotationId)
    ? rawDetail.annotationId
    : ''
  const body = typeof rawDetail?.body === 'string' && rawDetail.body.length <= 16 * 1024
    ? rawDetail.body
    : undefined
  if (raw.type === 'annotation-selected' && !selection) return null
  if (
    (raw.type === 'annotation-draft-change' || raw.type === 'annotation-submit')
    && (!annotationId || body === undefined)
  ) return null
  if (
    (raw.type === 'annotation-cancel' || raw.type === 'annotation-overlay-fallback')
    && !annotationId
  ) return null
  const detail = rawDetail
    ? {
        ...(typeof rawDetail.message === 'string' ? { message: rawDetail.message } : {}),
        ...(typeof rawDetail.path === 'string' ? { path: rawDetail.path } : {}),
        ...(typeof rawDetail.reason === 'string' ? { reason: rawDetail.reason } : {}),
        ...(annotationId ? { annotationId } : {}),
        ...(body !== undefined ? { body } : {}),
        ...(selection ? { selection } : {}),
        ...(typeof rawDetail.requestId === 'string' ? { requestId: rawDetail.requestId } : {}),
        ...(typeof rawDetail.permission === 'string'
          ? { permission: rawDetail.permission }
          : {}),
        ...(typeof rawDetail.requestingOrigin === 'string'
          ? { requestingOrigin: rawDetail.requestingOrigin }
          : {}),
        ...(typeof rawDetail.url === 'string' ? { url: rawDetail.url } : {}),
        ...(typeof rawDetail.title === 'string' ? { title: rawDetail.title } : {}),
        ...(typeof rawDetail.loading === 'boolean' ? { loading: rawDetail.loading } : {}),
        ...(typeof rawDetail.canGoBack === 'boolean'
          ? { canGoBack: rawDetail.canGoBack }
          : {}),
        ...(typeof rawDetail.canGoForward === 'boolean'
          ? { canGoForward: rawDetail.canGoForward }
          : {}),
        ...(typeof rawDetail.action === 'string' ? { action: rawDetail.action } : {}),
        ...(typeof rawDetail.code === 'string' ? { code: rawDetail.code } : {}),
      }
    : undefined
  return {
    version: raw.version,
    surfaceId: raw.surfaceId,
    type: raw.type as NativeWorkbenchSurfaceEventType,
    ...(detail ? { detail } : {}),
  }
}

function normalizeArtifactScreenshotResult(
  payload: unknown,
): import('./types').NativeArtifactScreenshotResult {
  const raw = payload && typeof payload === 'object'
    ? payload as Record<string, unknown>
    : {}
  if (raw.ok !== true || raw.method !== 'screenshot') {
    return {
      ok: false,
      method: 'screenshot',
      code: typeof raw.code === 'string' ? raw.code : 'operation-failed',
      message: typeof raw.message === 'string'
        ? raw.message
        : 'The Desktop artifact screenshot is unavailable.',
    }
  }
  const value = raw.value && typeof raw.value === 'object'
    ? raw.value as Record<string, unknown>
    : null
  const data = value?.data instanceof Uint8Array ? value.data : null
  const width = Number(value?.width)
  const height = Number(value?.height)
  if (
    value?.mime !== 'image/png'
    || !data
    || data.byteLength < 1
    || data.byteLength > 16 * 1024 * 1024
    || !Number.isSafeInteger(width)
    || !Number.isSafeInteger(height)
    || width < 1
    || height < 1
    || width > 32_768
    || height > 32_768
  ) {
    return {
      ok: false,
      method: 'screenshot',
      code: 'invalid-response',
      message: 'The Desktop artifact screenshot response is invalid.',
    }
  }
  return {
    ok: true,
    method: 'screenshot',
    value: { mime: 'image/png', data, width, height },
  }
}

function desktopNativeWorkbenchApi(api: OpenSquillaDesktopApi): NativeWorkbenchApi | undefined {
  if (
    typeof api.createWorkbenchSurface !== 'function'
    || typeof api.setWorkbenchSurfaceRect !== 'function'
    || typeof api.activateWorkbenchSurface !== 'function'
    || typeof api.destroyWorkbenchSurface !== 'function'
    || typeof api.onWorkbenchSurfaceEvent !== 'function'
  ) return undefined

  async function getCapabilities(): Promise<NativeWorkbenchCapabilities> {
    if (typeof api.getWorkbenchCapabilities !== 'function') {
      return { protocolVersions: [1], modes: ['offline'], maxSurfaces: 8 }
    }
    try {
      const payload = await api.getWorkbenchCapabilities()
      const raw = payload && typeof payload === 'object'
        ? payload as Record<string, unknown>
        : {}
      const versions = Array.isArray(raw.protocolVersions)
        ? raw.protocolVersions.filter(
            (value): value is 1 | 2 | 3 | 4 => (
              value === 1 || value === 2 || value === 3 || value === 4
            ),
          )
        : []
      const modes = Array.isArray(raw.modes)
        ? raw.modes.filter((value): value is 'full' | 'offline' =>
            value === 'full' || value === 'offline')
        : []
      return {
        protocolVersions: versions.length > 0 ? versions : [1],
        modes: modes.length > 0 ? modes : ['offline'],
        maxSurfaces: typeof raw.maxSurfaces === 'number' && Number.isFinite(raw.maxSurfaces)
          ? Math.max(1, Math.floor(raw.maxSurfaces))
          : 8,
      }
    } catch {
      return { protocolVersions: [1], modes: ['offline'], maxSurfaces: 8 }
    }
  }

  return {
    getCapabilities,
    ...(typeof api.getArtifactAnnotationCapabilities === 'function'
      ? {
          async getArtifactAnnotationCapabilities() {
            const payload = await api.getArtifactAnnotationCapabilities!()
            const raw = payload && typeof payload === 'object'
              ? payload as Record<string, unknown>
              : {}
            return {
              version: raw.version === 4 ? 4 as const : 3 as const,
              available: (raw.version === 3 || raw.version === 4) && raw.available === true,
              ...(typeof raw.picker === 'boolean' ? { picker: raw.picker } : {}),
              ...(typeof raw.trustedOverlay === 'boolean'
                ? { trustedOverlay: raw.trustedOverlay }
                : {}),
              ...(raw.overlayCopyVersion === 1 ? { overlayCopyVersion: 1 as const } : {}),
              ...(typeof raw.reason === 'string' ? { reason: raw.reason } : {}),
            }
          },
        }
      : {}),
    ...(typeof api.setArtifactAnnotationMode === 'function'
      ? { setArtifactAnnotationMode: payload => api.setArtifactAnnotationMode!(payload) }
      : {}),
    ...(typeof api.showArtifactAnnotationOverlay === 'function'
      ? { showArtifactAnnotationOverlay: payload => api.showArtifactAnnotationOverlay!(payload) }
      : {}),
    ...(typeof api.closeArtifactAnnotationOverlay === 'function'
      ? { closeArtifactAnnotationOverlay: payload => api.closeArtifactAnnotationOverlay!(payload) }
      : {}),
    ...(typeof api.screenshot === 'function'
      ? {
          async screenshot(payload) {
            return normalizeArtifactScreenshotResult(await api.screenshot!(payload))
          },
        }
      : {}),
    ...(typeof api.createArtifactPreviewLease === 'function'
      && typeof api.renewArtifactPreviewLease === 'function'
      && typeof api.revokeArtifactPreviewLease === 'function'
      ? {
          createArtifactPreviewLease: payload => api.createArtifactPreviewLease!(payload),
          renewArtifactPreviewLease: payload => api.renewArtifactPreviewLease!(payload),
          revokeArtifactPreviewLease: payload => api.revokeArtifactPreviewLease!(payload),
        }
      : {}),
    createSurface: payload => api.createWorkbenchSurface!(payload),
    setSurfaceRect: payload => api.setWorkbenchSurfaceRect!(payload),
    activateSurface: surfaceId => api.activateWorkbenchSurface!(surfaceId),
    destroySurface: surfaceId => api.destroyWorkbenchSurface!(surfaceId),
    ...(typeof api.navigateWorkbenchSurface === 'function'
      ? { navigateSurface: payload => api.navigateWorkbenchSurface!(payload) }
      : {}),
    ...(typeof api.respondToWorkbenchPermission === 'function'
      ? { respondToPermission: payload => api.respondToWorkbenchPermission!(payload) }
      : {}),
    onSurfaceEvent(callback) {
      return api.onWorkbenchSurfaceEvent!((payload) => {
        const event = normalizeNativeSurfaceEvent(payload)
        if (event) callback(event)
      })
    },
  }
}

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
  const desktopApi = requireDesktopApi()
  const nativeWorkbench = desktopNativeWorkbenchApi(desktopApi)
  return {
    id: 'desktop',
    capabilities: {
      ...desktopCapabilities,
      hasNativeWorkbenchSurfaces: nativeWorkbench !== undefined,
    },
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
      ...(typeof window.opensquillaDesktop?.reportSandboxUnavailable === 'function'
        ? {
            reportSandboxUnavailable: (payload) => (
              requireDesktopApi().reportSandboxUnavailable!(payload)
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
      async chooseProjectDirectory(request) {
        const api = requireDesktopApi()
        if (typeof api.chooseProjectDirectory !== 'function') return null
        return api.chooseProjectDirectory(request)
      },
    },
    workbench: {
      ...(nativeWorkbench ? { native: nativeWorkbench } : {}),
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
