export type PlatformId = 'web' | 'desktop'

export interface GatewayStatus {
  url: string
  port: number
  owned: boolean
  status: 'starting' | 'ready' | 'stopped' | 'error'
  logPath: string
  error?: string
}

export interface DesktopRetryStartupResult {
  ok: boolean
  error?: string
}

export type DesktopUpdateStatus =
  | 'idle'
  | 'checking'
  | 'available'
  | 'downloading'
  | 'downloaded'
  | 'not-available'
  | 'error'
  | 'applying'

export type DesktopUpdateInstallMode = 'native' | 'manual' | 'unsupported'
export type DesktopUpdateSource = 'oss' | 'github'
export type DesktopUpdateErrorCode =
  | 'source_unreachable'
  | 'manifest_invalid'
  | 'checksum_unavailable'
  | 'integrity_failed'
  | 'download_failed'
  | 'install_failed'

export interface DesktopUpdateState {
  status: DesktopUpdateStatus
  currentVersion: string
  latestVersion: string | null
  progress: number | null
  checkedAt: string | null
  error: string | null
  errorCode: DesktopUpdateErrorCode | null
  snoozedUntil: string | null
  canCheck: boolean
  canNativeInstall: boolean
  installMode: DesktopUpdateInstallMode
  releaseUrl: string | null
  source: DesktopUpdateSource | null
  fallbackUsed: boolean
}

export interface DesktopSettings {
  provider: string
  model: string
  baseUrl: string
  apiKeyConfigured: boolean
  searchProvider: string
  searchApiKeyEnv: string
  searchApiKeyConfigured: boolean
  disableNetworkObservability: boolean
  searchProviders?: SearchProviderOption[]
  providers?: ProviderOption[]
  gateway: GatewayStatus
}

export interface ProviderOption {
  providerId: string
  label: string
  model?: string
  baseUrl?: string
  requiresApiKey?: boolean
  note?: string
}

export interface SearchProviderOption {
  providerId: string
  label: string
  envKey?: string
  requiresApiKey?: boolean
  note?: string
  keyPlaceholder?: string
}

export interface DesktopSettingsPayload {
  provider?: string
  model?: string
  baseUrl?: string
  apiKey?: string
  searchProvider?: string
  searchApiKey?: string
  disableNetworkObservability?: boolean
}

export type DesktopMainWindowCloseBehavior = 'background' | 'quit' | 'ask'
export type WorkbenchPreviewMode = 'full' | 'offline'

export interface DesktopPreferences {
  schemaVersion?: number
  mainWindowCloseBehavior: DesktopMainWindowCloseBehavior
  canRunInBackground: boolean
  platform: 'darwin' | 'win32' | 'linux' | 'other'
  workbenchPreviewMode?: WorkbenchPreviewMode
  effectiveWorkbenchPreviewMode?: WorkbenchPreviewMode
  workbenchPreviewNoticeShown?: boolean
  workbenchPreviewForcedOffline?: boolean
  sandboxUnavailableWarningSuppressed?: boolean
}

export interface PlatformCapabilities {
  isDesktop: boolean
  ownsGateway: boolean
  canManageLocalApiKeys: boolean
  canRevealGatewayLog: boolean
  canRestartGateway: boolean
  hasDesktopOnboarding: boolean
  hasWebConfig: boolean
  /**
   * The operator likely has a terminal where `opensquilla` resolves (web
   * installs are CLI-launched). Desktop is false: copyable CLI commands fold
   * behind an advanced disclosure and get rewritten to the shell-reported
   * invocation prefix so they run against the app's config and state roots.
   */
  hasTerminalWorkflow: boolean
  /**
   * The host can open a generated artifact with the OS default application.
   * Set on desktop, where `window.open` is denied by the shell handler so the
   * in-browser blob-popup path can never succeed.
   */
  canOpenArtifactsNatively: boolean
  /** The shell can host isolated native Workbench WebContents surfaces. */
  hasNativeWorkbenchSurfaces: boolean
}

export interface ArtifactOpenRequest {
  /** Raw artifact bytes, already fetched (and authenticated) by the renderer. */
  data: ArrayBuffer
  /** Original filename; its extension drives the OS default-app association. */
  name: string
  /** Content-Type, used as a fallback when the name carries no extension. */
  mime: string
}

export interface ArtifactNativeOpenResult {
  ok: boolean
  message?: string
}

export interface ProjectDirectoryPickerRequest {
  /** Directory the native picker should reveal when it opens. */
  initialPath?: string
}

export interface PlatformFilesApi {
  /** Write the bytes to a temp file and open it with the OS default app. */
  openArtifact?: (payload: ArtifactOpenRequest) => Promise<ArtifactNativeOpenResult>
  /** Open the trusted host's native folder picker. Undefined on the web. */
  chooseProjectDirectory?: (
    request?: ProjectDirectoryPickerRequest,
  ) => Promise<{ path: string } | null>
}

export interface NativeWorkbenchCreateSurfaceRequestV1 {
  version: 1
  surfaceId: string
  kind: 'artifact-html'
  payload: {
    /** HTML bytes fetched through the renderer's authenticated Artifact client. */
    data: ArrayBuffer
    name: string
    mime: string
    scopeId: string
    /** Explicit, per-surface user choice. Defaults to false in the UI. */
    allowRemoteResources: boolean
  }
}

export interface NativeWorkbenchCreateArtifactSurfaceRequestV2 {
  version: 2 | 3
  surfaceId: string
  kind: 'artifact-preview'
  payload: {
    launchUrl: string
    expectedOrigin: string
    scopeId: string
    mode: WorkbenchPreviewMode
  }
}

export interface NativeWorkbenchCreateUrlSurfaceRequestV2 {
  version: 2 | 3
  surfaceId: string
  kind: 'url-preview'
  payload: {
    url: string
    scopeId: string
  }
}

export type NativeWorkbenchCreateSurfaceRequest =
  | NativeWorkbenchCreateSurfaceRequestV1
  | NativeWorkbenchCreateArtifactSurfaceRequestV2
  | NativeWorkbenchCreateUrlSurfaceRequestV2

export interface NativeWorkbenchCapabilities {
  protocolVersions: Array<1 | 2 | 3>
  modes: WorkbenchPreviewMode[]
  maxSurfaces: number
}

export interface NativeArtifactAnnotationCapabilities {
  version: 3
  available: boolean
  picker?: boolean
  trustedOverlay?: boolean
  overlayCopyVersion?: 1
  reason?: string
}

export interface NativeArtifactAnnotationModeRequest {
  version: 3
  surfaceId: string
  enabled: boolean
}

export interface NativeArtifactAnnotationOverlayRequest {
  version: 3
  surfaceId: string
  selectionId: string
  annotationId: string
  initialBody?: string
  overlayCopyVersion?: 1
  copy?: {
    targetLabel: string
    contextLabel: string
    bodyLabel: string
    placeholder: string
    newlineHint: string
    cancelLabel: string
    submitLabel: string
    emptyBodyMessage: string
  }
}

export interface NativeArtifactAnnotationOverlayCloseRequest {
  version: 3
  surfaceId: string
  annotationId?: string
}

export interface NativeArtifactScreenshotRequest {
  version: 3
}

export interface NativeArtifactScreenshotValue {
  mime: 'image/png'
  data: Uint8Array
  width: number
  height: number
}

export type NativeArtifactScreenshotResult = {
  ok: true
  method: 'screenshot'
  value: NativeArtifactScreenshotValue
} | {
  ok: false
  method: 'screenshot'
  code: string
  message: string
}

export interface NativeArtifactAnnotationSelection {
  selectionId: string
  tagName: string
  elementPath: string
  elementProofSha256: string
  /** Compatibility diagnostic emitted by current Desktop shells. */
  domSha256?: string
  rect: { x: number; y: number; width: number; height: number }
}

export interface NativeWorkbenchSurfaceRectRequest {
  surfaceId: string
  x: number
  y: number
  width: number
  height: number
  visible: boolean
}

export interface NativeWorkbenchSurfaceResult {
  ok: boolean
  code?: string
  retryable?: boolean
  message?: string
}

export type NativeWorkbenchSurfaceEventType =
  | 'loading'
  | 'ready'
  | 'missing-resource'
  | 'navigation-state'
  | 'permission-request'
  | 'blocked-action'
  | 'capability-expired'
  | 'unresponsive'
  | 'responsive'
  | 'error'
  | 'crashed'
  | 'escape'
  | 'annotation-selected'
  | 'annotation-draft-change'
  | 'annotation-submit'
  | 'annotation-cancel'
  | 'annotation-overlay-fallback'

export interface NativeWorkbenchSurfaceEvent {
  version: 1 | 2 | 3
  surfaceId: string
  type: NativeWorkbenchSurfaceEventType
  detail?: {
    requestId?: string
    permission?: string
    requestingOrigin?: string
    url?: string
    title?: string
    loading?: boolean
    canGoBack?: boolean
    canGoForward?: boolean
    action?: string
    code?: string
    message?: string
    path?: string
    reason?: string
    annotationId?: string
    selection?: NativeArtifactAnnotationSelection
    body?: string
  }
}

export interface NativeWorkbenchNavigateRequest {
  version: 2 | 3
  surfaceId: string
  action: 'back' | 'forward' | 'reload' | 'stop' | 'navigate'
  url?: string
}

export interface NativeWorkbenchPermissionResponse {
  version: 2 | 3
  surfaceId: string
  requestId: string
  allow: boolean
}

export interface NativeArtifactPreviewLeaseCreateRequest {
  version: 1
  artifactId: string
  scopeId: string
  mode: WorkbenchPreviewMode
  authToken?: string
}

export interface NativeArtifactPreviewLeaseControlRequest {
  version: 1
  leaseId: string
  scopeId: string
  authToken?: string
}

export type NativeArtifactPreviewLeaseBrokerResult = {
  ok: true
  status: number
  payload: unknown
} | {
  ok: false
  status: number
  code: string
  message: string
}

export interface NativeWorkbenchApi {
  getCapabilities?(): Promise<NativeWorkbenchCapabilities>
  getArtifactAnnotationCapabilities?(): Promise<NativeArtifactAnnotationCapabilities>
  setArtifactAnnotationMode?(
    request: NativeArtifactAnnotationModeRequest,
  ): Promise<NativeWorkbenchSurfaceResult>
  showArtifactAnnotationOverlay?(
    request: NativeArtifactAnnotationOverlayRequest,
  ): Promise<NativeWorkbenchSurfaceResult>
  closeArtifactAnnotationOverlay?(
    request: NativeArtifactAnnotationOverlayCloseRequest,
  ): Promise<NativeWorkbenchSurfaceResult>
  screenshot?(
    request: NativeArtifactScreenshotRequest,
  ): Promise<NativeArtifactScreenshotResult>
  createArtifactPreviewLease?(
    request: NativeArtifactPreviewLeaseCreateRequest,
  ): Promise<NativeArtifactPreviewLeaseBrokerResult>
  renewArtifactPreviewLease?(
    request: NativeArtifactPreviewLeaseControlRequest,
  ): Promise<NativeArtifactPreviewLeaseBrokerResult>
  revokeArtifactPreviewLease?(
    request: NativeArtifactPreviewLeaseControlRequest,
  ): Promise<NativeArtifactPreviewLeaseBrokerResult>
  createSurface(
    request: NativeWorkbenchCreateSurfaceRequest,
  ): Promise<NativeWorkbenchSurfaceResult>
  setSurfaceRect(
    request: NativeWorkbenchSurfaceRectRequest,
  ): Promise<NativeWorkbenchSurfaceResult>
  activateSurface(surfaceId: string): Promise<NativeWorkbenchSurfaceResult>
  destroySurface(surfaceId: string): Promise<NativeWorkbenchSurfaceResult>
  navigateSurface?(
    request: NativeWorkbenchNavigateRequest,
  ): Promise<NativeWorkbenchSurfaceResult>
  respondToPermission?(
    request: NativeWorkbenchPermissionResponse,
  ): Promise<NativeWorkbenchSurfaceResult>
  onSurfaceEvent(callback: (event: NativeWorkbenchSurfaceEvent) => void): () => void
}

export interface PlatformWorkbenchApi {
  /**
   * Undefined on web and on older desktop shells. Callers must keep a DOM
   * sandbox fallback for HTML Artifact preview.
   */
  native?: NativeWorkbenchApi
}

export interface CliInvocation {
  mode: 'bundled' | 'dev'
  /** Paste-ready replacement for the leading `opensquilla` CLI token. */
  prefix: string
}

export interface PlatformGatewayApi {
  getStatus(): Promise<GatewayStatus>
  revealLog?: () => Promise<boolean>
  retryStartup?: () => Promise<DesktopRetryStartupResult>
  /** null when the shell predates the bridge or the lookup fails; callers
   *  fall back to the raw command. */
  getCliInvocation?: () => Promise<CliInvocation | null>
}

export interface PlatformSettingsApi {
  getDesktopSettings?: () => Promise<DesktopSettings>
  saveDesktopSettings?: (payload: DesktopSettingsPayload) => Promise<DesktopSettings>
  resetDesktopSettings?: () => Promise<{ ok: boolean }>
  getDesktopPreferences?: () => Promise<DesktopPreferences>
  saveDesktopPreferences?: (
    payload: {
      mainWindowCloseBehavior?: DesktopMainWindowCloseBehavior
      workbenchPreviewMode?: WorkbenchPreviewMode
      workbenchPreviewNoticeShown?: boolean
      sandboxUnavailableWarningSuppressed?: boolean
    },
  ) => Promise<DesktopPreferences>
  reportSandboxUnavailable?: (
    payload: { state: 'failed' | 'unavailable'; message?: string },
  ) => Promise<{ shown: boolean; suppressed: boolean }>
}

export interface PlatformOnboardingApi {
  getDefaults?: () => Promise<unknown>
  save?: (payload: unknown) => Promise<unknown>
  cancel?: () => Promise<unknown>
}

export interface PlatformUpdatesApi {
  getState(): Promise<DesktopUpdateState>
  check(): Promise<DesktopUpdateState>
  download(): Promise<DesktopUpdateState>
  relaunch(): Promise<DesktopUpdateState>
  dismiss(): Promise<DesktopUpdateState>
  onState(callback: (state: DesktopUpdateState) => void): () => void
}

export interface Platform {
  id: PlatformId
  capabilities: PlatformCapabilities
  gateway: PlatformGatewayApi
  settings: PlatformSettingsApi
  onboarding: PlatformOnboardingApi
  files: PlatformFilesApi
  workbench: PlatformWorkbenchApi
  updates: PlatformUpdatesApi
  /**
   * The host OS locale (BCP-47), used only to seed the initial UI language on
   * first run. Desktop reads it from Electron's app.getLocale(); web returns
   * undefined so the renderer falls back to navigator.language.
   */
  getOsLocale: () => Promise<string | undefined>
  setNativeTheme: (payload: { source: 'light' | 'dark' | 'system' }) => Promise<unknown>
  /**
   * Whether THIS host applies updates natively (electron-updater). Web always
   * returns false; desktop returns the shell's live native-update capability,
   * including runtime guards such as macOS requiring /Applications.
   * Presentation ownership is intentionally reported separately by
   * desktopUpdateManaged(), since unsigned Windows can discover an update and
   * open a manual installer without applying it natively.
   */
  nativeAutoUpdateEnabled: () => Promise<boolean>
  /**
   * Whether the desktop shell owns update discovery and presentation, including
   * manual versioned installers on unsigned Windows builds. This is deliberately
   * separate from nativeAutoUpdateEnabled so the passive gateway banner does not
   * duplicate the shell-managed Windows notice.
   */
  desktopUpdateManaged: () => Promise<boolean>
}
