import { detectPlatformId } from './capabilities'
import { createDesktopPlatform } from './desktop'
import type { Platform } from './types'
import { createWebPlatform } from './web'
import { localAccountRequest } from '@/adapters/gateway/localAccountV1'
import { accountStylePreviewRequest } from './accountStylePreview'

let cachedPlatform: Platform | null = null

export function getPlatform(): Platform {
  const platformId = detectPlatformId()
  if (cachedPlatform?.id === platformId) return cachedPlatform
  const accountPreview = import.meta.env.DEV
    && import.meta.env.VITE_LOCAL_ACCOUNT_PREVIEW === '1'
    && typeof window !== 'undefined'
    && new URLSearchParams(window.location.search).get('accountMock') === '1'
  cachedPlatform = platformId === 'desktop'
    ? createDesktopPlatform()
    : createWebPlatform(accountPreview ? accountStylePreviewRequest
      : import.meta.env.VITE_LOCAL_ACCOUNT_PREVIEW === '1' ? localAccountRequest : undefined)
  return cachedPlatform
}

export function usePlatform(): Platform {
  return getPlatform()
}

export type {
  ArtifactNativeOpenResult,
  ArtifactOpenRequest,
  CliInvocation,
  DesktopMainWindowCloseBehavior,
  DesktopGatewayConnection,
  DesktopResumeEvent,
  DesktopResumeSource,
  DesktopPreferences,
  DesktopSettings,
  DesktopSettingsPayload,
  DesktopUpdateState,
  DesktopUpdateStatus,
  DesktopUpdateErrorCode,
  DesktopUpdateInstallMode,
  DesktopUpdateSource,
  GatewayStatus,
  NativeWorkbenchApi,
  NativeWorkbenchCreateSurfaceRequest,
  NativeWorkbenchSurfaceEvent,
  NativeWorkbenchSurfaceRectRequest,
  NativeWorkbenchSurfaceResult,
  Platform,
  PlatformCapabilities,
  PlatformFilesApi,
  PlatformGatewayApi,
  PlatformId,
  PlatformOnboardingApi,
  PlatformUpdatesApi,
  PlatformSettingsApi,
  SearchProviderOption,
  WorkbenchPreviewMode,
  SandboxUpgradeReport,
} from './types'
