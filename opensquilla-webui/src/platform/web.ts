import { webCapabilities } from './capabilities'
import type { DesktopUpdateState, GatewayStatus, Platform } from './types'

const unavailableGateway: GatewayStatus = {
  url: '',
  port: 0,
  owned: false,
  status: 'stopped',
  logPath: '',
}

const unavailableUpdate: DesktopUpdateState = {
  status: 'idle',
  currentVersion: '',
  latestVersion: null,
  progress: null,
  checkedAt: null,
  error: null,
  errorCode: null,
  snoozedUntil: null,
  canCheck: false,
  canNativeInstall: false,
  installMode: 'unsupported',
  releaseUrl: null,
  source: null,
  fallbackUsed: false,
}

async function webUpdateState(): Promise<DesktopUpdateState> {
  return { ...unavailableUpdate }
}

export function createWebPlatform(): Platform {
  return {
    id: 'web',
    capabilities: webCapabilities,
    async getOsLocale() {
      return undefined
    },
    async setNativeTheme() {
      return undefined
    },
    async nativeAutoUpdateEnabled() {
      return false
    },
    async desktopUpdateManaged() {
      return false
    },
    gateway: {
      async getStatus() {
        return { ...unavailableGateway }
      },
    },
    settings: {},
    onboarding: {},
    files: {},
    updates: {
      getState: webUpdateState,
      check: webUpdateState,
      download: webUpdateState,
      relaunch: webUpdateState,
      dismiss: webUpdateState,
      onState: () => () => undefined,
    },
  }
}
