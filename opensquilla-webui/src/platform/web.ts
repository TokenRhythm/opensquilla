import { webCapabilities } from './capabilities'
import { createWebAccount, type WebAccountRequest } from './webAccount'
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

export function createWebPlatform(accountRequest?: WebAccountRequest): Platform {
  return {
    id: 'web',
    account: accountRequest ? createWebAccount(accountRequest) : undefined,
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
    migration: {},
    window: {},
    files: {},
    workbench: {},
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
