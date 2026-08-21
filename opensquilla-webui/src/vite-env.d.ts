/// <reference types="vite/client" />

import type {
  ArtifactNativeOpenResult,
  ArtifactOpenRequest,
  DesktopMainWindowCloseBehavior,
  DesktopPreferences,
  DesktopRetryStartupResult,
  DesktopUpdateState,
  DesktopSettings,
  DesktopSettingsPayload,
  NativeArtifactPreviewLeaseBrokerResult,
  NativeArtifactPreviewLeaseControlRequest,
  NativeArtifactPreviewLeaseCreateRequest,
  NativeWorkbenchCreateSurfaceRequest,
  NativeWorkbenchNavigateRequest,
  NativeWorkbenchPermissionResponse,
  NativeWorkbenchSurfaceEvent,
  NativeWorkbenchSurfaceRectRequest,
  NativeWorkbenchSurfaceResult,
  ProjectDirectoryPickerRequest,
  WorkbenchPreviewMode,
} from './platform/types'

type MigrationSourceKind = 'cli-home' | 'desktop-home' | 'windows-portable'
type DesktopCleanupMode = 'reset-current-settings' | 'delete-current-profile' | 'delete-all-user-data'
interface DesktopCleanupReport {
  schema_version: 1
  outcome: 'ready' | 'blocked' | 'complete' | 'partial'
  stable_code: string
  mode: DesktopCleanupMode
  items: Array<{ kind: string; path: string; exists: boolean; identity: string | null }>
  transaction_id: string
  revision: number
  scope_fingerprint: string
}

declare global {
  interface OpenSquillaDesktopApi {
    getOsLocale: () => Promise<string | undefined>
    isAutoUpdateEnabled: () => Promise<boolean>
    isDesktopUpdateManaged?: () => Promise<boolean>
    getUpdateState?: () => Promise<DesktopUpdateState>
    checkForUpdates?: () => Promise<DesktopUpdateState>
    downloadUpdate?: () => Promise<DesktopUpdateState>
    relaunchToUpdate?: () => Promise<DesktopUpdateState>
    dismissUpdate?: () => Promise<DesktopUpdateState>
    onUpdateState?: (callback: (payload: unknown) => void) => () => void
    getGatewayStatus: () => Promise<DesktopSettings['gateway']>
    getCliInvocation?: () => Promise<unknown>
    revealGatewayLog: () => Promise<boolean>
    getDesktopSettings: () => Promise<DesktopSettings>
    saveDesktopSettings: (payload: DesktopSettingsPayload) => Promise<DesktopSettings>
    resetDesktopSettings: () => Promise<{ ok: boolean }>
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
    onWindowHidden?: (callback: () => void) => () => void
    inspectDesktopCleanup?: (payload: { mode: DesktopCleanupMode }) => Promise<{
      ok: boolean
      previewId: string | null
      report: DesktopCleanupReport
      profile: { kind: 'primary'; recoveryId: null }
    }>
    discardDesktopCleanup?: (payload: { previewId: string }) => Promise<boolean>
    applyDesktopCleanup?: (payload: {
      previewId: string
      acknowledged: boolean
      confirmation: string
    }) => Promise<{
      ok: boolean
      aborted?: boolean
      scheduled?: boolean
      partial?: boolean
      previewId?: string | null
      report?: DesktopCleanupReport
      profile?: { kind: 'primary'; recoveryId: null }
      detail?: string
    }>
    revealDesktopUserData?: () => Promise<boolean>
    abandonCleanupTransaction?: () => Promise<unknown>
    setNativeTheme?: (payload: { source: 'light' | 'dark' | 'system' }) => Promise<unknown>
    openArtifact: (payload: ArtifactOpenRequest) => Promise<ArtifactNativeOpenResult>
    chooseProjectDirectory: (
      request?: ProjectDirectoryPickerRequest,
    ) => Promise<{ path: string } | null>
    createWorkbenchSurface?: (
      payload: NativeWorkbenchCreateSurfaceRequest,
    ) => Promise<NativeWorkbenchSurfaceResult>
    createArtifactPreviewLease?: (
      payload: NativeArtifactPreviewLeaseCreateRequest,
    ) => Promise<NativeArtifactPreviewLeaseBrokerResult>
    renewArtifactPreviewLease?: (
      payload: NativeArtifactPreviewLeaseControlRequest,
    ) => Promise<NativeArtifactPreviewLeaseBrokerResult>
    revokeArtifactPreviewLease?: (
      payload: NativeArtifactPreviewLeaseControlRequest,
    ) => Promise<NativeArtifactPreviewLeaseBrokerResult>
    getWorkbenchCapabilities?: () => Promise<unknown>
    getArtifactAnnotationCapabilities?: () => Promise<unknown>
    setArtifactAnnotationMode?: (
      payload: import('./platform/types').NativeArtifactAnnotationModeRequest,
    ) => Promise<import('./platform/types').NativeWorkbenchSurfaceResult>
    showArtifactAnnotationOverlay?: (
      payload: import('./platform/types').NativeArtifactAnnotationOverlayRequest,
    ) => Promise<import('./platform/types').NativeWorkbenchSurfaceResult>
    closeArtifactAnnotationOverlay?: (
      payload: import('./platform/types').NativeArtifactAnnotationOverlayCloseRequest,
    ) => Promise<import('./platform/types').NativeWorkbenchSurfaceResult>
    screenshot?: (
      payload: import('./platform/types').NativeArtifactScreenshotRequest,
    ) => Promise<unknown>
    navigateWorkbenchSurface?: (
      payload: NativeWorkbenchNavigateRequest,
    ) => Promise<NativeWorkbenchSurfaceResult>
    respondToWorkbenchPermission?: (
      payload: NativeWorkbenchPermissionResponse,
    ) => Promise<NativeWorkbenchSurfaceResult>
    setWorkbenchSurfaceRect?: (
      payload: NativeWorkbenchSurfaceRectRequest,
    ) => Promise<NativeWorkbenchSurfaceResult>
    activateWorkbenchSurface?: (surfaceId: string) => Promise<NativeWorkbenchSurfaceResult>
    destroyWorkbenchSurface?: (surfaceId: string) => Promise<NativeWorkbenchSurfaceResult>
    onWorkbenchSurfaceEvent?: (
      callback: (payload: NativeWorkbenchSurfaceEvent) => void,
    ) => () => void
    getOnboardingDefaults: () => Promise<unknown>
    saveOnboarding: (payload: unknown) => Promise<unknown>
    cancelOnboarding: () => Promise<unknown>
    getBootState: () => Promise<unknown>
    getRecoveryState?: () => Promise<unknown>
    retryProfileConsolidation?: () => Promise<{ ok: boolean; error?: string }>
    chooseLegacyAgentDataLocation?: (payload?: Record<string, never>) => Promise<unknown>
    retryStartup: () => Promise<DesktopRetryStartupResult>
    quitApp: () => Promise<unknown>
    migrationSummary?: (payload?: { source?: string }) => Promise<unknown>
    migrationBrowseSource?: (payload: { kind: MigrationSourceKind }) => Promise<unknown>
    migrationRun?: (payload: { previewId: string; overwrite?: boolean }) => Promise<unknown>
    migrationTakeLastResult?: () => Promise<unknown>
    migrationPeekLastResult?: () => Promise<unknown>
    migrationDismissLastResult?: () => Promise<unknown>
    revealRecoveryPath?: (payload: {
      target: 'primary' | 'backups'
    }) => Promise<boolean>
    onBootStatus: (callback: (payload: unknown) => void) => () => void
    onBootError: (callback: (payload: unknown) => void) => () => void
    onMigrationProgress?: (callback: (payload: unknown) => void) => () => void
  }

  interface Window {
    opensquillaDesktop?: OpenSquillaDesktopApi
  }
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

export {}
