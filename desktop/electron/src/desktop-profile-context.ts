import { lstatSync, readdirSync, realpathSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'

export type DesktopProfileKind = 'primary' | 'recovery'

export interface DesktopProfilePaths {
  kind: DesktopProfileKind
  recoveryId: string | null
  home: string
  credentialPath: string
  logsDir: string
}

const RECOVERY_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

/**
 * The Desktop runtime has one authoritative profile. Historical recovery
 * profiles are never returned from this function and cannot become active.
 */
export function primaryProfilePaths(userData: string): DesktopProfilePaths {
  const root = resolve(userData)
  return {
    kind: 'primary',
    recoveryId: null,
    home: join(root, 'opensquilla'),
    credentialPath: join(root, 'desktop-credential.json'),
    logsDir: join(root, 'logs'),
  }
}

export function isRecoveryProfileId(value: unknown): value is string {
  return typeof value === 'string' && RECOVERY_ID_RE.test(value)
}

export function recoveryProfilePaths(userData: string, recoveryId: string): DesktopProfilePaths {
  if (!isRecoveryProfileId(recoveryId)) throw new Error('Invalid recovery profile id.')
  const root = join(resolve(userData), 'recovery-profiles', recoveryId)
  return {
    kind: 'recovery',
    recoveryId,
    home: join(root, 'opensquilla'),
    credentialPath: join(root, 'desktop-credential.json'),
    logsDir: join(root, 'logs'),
  }
}

function realDirectoryStatus(path: string): 'valid' | 'missing' | 'unsafe' {
  try {
    const info = lstatSync(path)
    return info.isDirectory() && !info.isSymbolicLink() ? 'valid' : 'unsafe'
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'ENOENT' ? 'missing' : 'unsafe'
  }
}

function recoveryProfileStatus(
  userData: string,
  profile: DesktopProfilePaths,
): 'valid' | 'missing' | 'unsafe' {
  const recoveryRoot = join(resolve(userData), 'recovery-profiles')
  const profileRoot = dirname(profile.home)
  for (const path of [recoveryRoot, profileRoot, profile.home]) {
    const status = realDirectoryStatus(path)
    if (status !== 'valid') return status
  }
  try {
    const resolvedRecoveryRoot = realpathSync(recoveryRoot)
    const resolvedProfileRoot = realpathSync(profileRoot)
    const resolvedHome = realpathSync(profile.home)
    if (dirname(resolvedProfileRoot) !== resolvedRecoveryRoot) return 'unsafe'
    if (dirname(resolvedHome) !== resolvedProfileRoot) return 'unsafe'
    return 'valid'
  } catch {
    return 'unsafe'
  }
}

/**
 * Safely enumerate legacy recovery profiles for one-time consolidation.
 *
 * This is deliberately not a profile-selection API: callers receive paths only
 * after the UUID and every no-follow directory boundary are verified. A
 * symlink, junction-like alias, missing home, or malformed entry is ignored and
 * is never traversed.
 */
export function allProfileContexts(userData: string): DesktopProfilePaths[] {
  const profiles = [primaryProfilePaths(userData)]
  const recoveryRoot = join(resolve(userData), 'recovery-profiles')
  let entries: string[] = []
  try {
    const rootInfo = lstatSync(recoveryRoot)
    if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) return profiles
    entries = readdirSync(recoveryRoot)
  } catch {
    return profiles
  }
  for (const entry of entries.sort()) {
    if (!isRecoveryProfileId(entry)) continue
    const profile = recoveryProfilePaths(userData, entry)
    if (recoveryProfileStatus(userData, profile) !== 'valid') continue
    profiles.push(profile)
  }
  return profiles
}
