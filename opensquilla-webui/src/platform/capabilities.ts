import type { PlatformCapabilities, PlatformId } from './types'

export const webCapabilities: PlatformCapabilities = {
  isDesktop: false,
  ownsGateway: false,
  canManageLocalApiKeys: false,
  canRevealGatewayLog: false,
  canRestartGateway: false,
  hasDesktopOnboarding: false,
  hasWebConfig: true,
  hasTerminalWorkflow: true,
  canOpenArtifactsNatively: false,
  hasNativeWorkbenchSurfaces: false,
}

export const desktopCapabilities: PlatformCapabilities = {
  isDesktop: true,
  ownsGateway: true,
  canManageLocalApiKeys: true,
  canRevealGatewayLog: true,
  canRestartGateway: true,
  hasDesktopOnboarding: true,
  // Desktop now renders the same RPC-backed SettingsDialog as web (its local
  // gateway serves the same Control UI RPC); a desktop-only Runtime section adds
  // the owned-gateway controls. See router/index.ts + SettingsDialog.
  hasWebConfig: true,
  // Desktop users did not install a CLI: the gateway binary ships inside the
  // app bundle, off PATH. Command blocks fold away and rewrite instead.
  hasTerminalWorkflow: false,
  canOpenArtifactsNatively: true,
  hasNativeWorkbenchSurfaces: true,
}

export function detectPlatformId(): PlatformId {
  return typeof window !== 'undefined' && window.opensquillaDesktop ? 'desktop' : 'web'
}
