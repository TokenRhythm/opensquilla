import { detectPlatformId } from './capabilities'
import { createDesktopPlatform } from './desktop'
import type { Platform } from './types'
import { createWebPlatform } from './web'

let cachedPlatform: Platform | null = null

export function getPlatform(): Platform {
  const platformId = detectPlatformId()
  if (cachedPlatform?.id === platformId) return cachedPlatform
  cachedPlatform = platformId === 'desktop'
    ? createDesktopPlatform()
    : createWebPlatform()
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
  DesktopPreferences,
  DesktopSettings,
  DesktopSettingsPayload,
  DesktopUpdateState,
  DesktopUpdateStatus,
  DesktopUpdateErrorCode,
  DesktopUpdateInstallMode,
  DesktopUpdateSource,
  GatewayStatus,
  Platform,
  PlatformCapabilities,
  PlatformFilesApi,
  PlatformGatewayApi,
  PlatformId,
  PlatformOnboardingApi,
  PlatformUpdatesApi,
  PlatformSettingsApi,
  SearchProviderOption,
} from './types'
