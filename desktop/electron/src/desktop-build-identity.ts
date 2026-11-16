import { join } from 'node:path'

import { channelUserDataPath } from './desktop-profile-context.js'

export interface DesktopBuildIdentity {
  name: string
  appId: string
  scheme: string
  isolated: boolean
  updatesEnabled: boolean
}

/** Packaged preview clients use their own installation and local state namespace. */
export function resolveDesktopBuildIdentity(metadata: unknown): DesktopBuildIdentity {
  const distribution = metadata && typeof metadata === 'object'
    ? (metadata as Record<string, unknown>).opensquillaDistribution : undefined
  if (distribution === 'preview') {
    return {
      name: 'OpenSquilla Preview',
      appId: 'ai.opensquilla.desktop.preview',
      scheme: 'opensquilla-preview',
      isolated: true,
      updatesEnabled: false,
    }
  }
  if (distribution !== undefined) throw new Error('Unsupported desktop distribution')
  return {
    name: 'OpenSquilla',
    appId: 'ai.opensquilla.desktop',
    scheme: 'opensquilla',
    isolated: false,
    updatesEnabled: true,
  }
}

export function desktopBuildUserDataPath(
  identity: DesktopBuildIdentity, appData: string, userData: string, version: string,
): string {
  return identity.isolated
    ? join(appData, 'opensquilla-preview')
    : channelUserDataPath(userData, version)
}
