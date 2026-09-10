import { createHash, randomUUID } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { lstat, mkdir, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { UPDATE_GITHUB_RELEASE_PAGE_ROOT, type DesktopUpdateCandidate } from './update-channel.js'
import { parseOpenSquillaReleaseTag } from './update-feed-resolver.js'
import { verifyWindowsInstaller, WindowsUpdateSecurityError } from './windows-update-security.js'

export interface WindowsUpdateCacheDescriptor {
  schemaVersion: 1
  tag: string
  version: string
  installer: string
  sha256: string
  bytes: number
}

export const WINDOWS_UPDATE_CACHE_DESCRIPTOR = 'windows-update-cache.json'
const MAX_INSTALLER_BYTES = 4 * 1024 * 1024 * 1024

function validDescriptor(value: unknown): value is WindowsUpdateCacheDescriptor {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const item = value as Record<string, unknown>
  if (item.schemaVersion !== 1 || typeof item.version !== 'string'
    || !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc(0|[1-9]\d*))?$/.test(item.version)) return false
  const parsed = parseOpenSquillaReleaseTag(item.version)
  if (!parsed || parsed.base.split('.').some((part) => !Number.isSafeInteger(Number(part)))
    || (parsed.rc !== null && !Number.isSafeInteger(parsed.rc))) return false
  const tag = parsed.rc === null ? `v${parsed.base}` : `v${parsed.base}rc${parsed.rc}`
  return item.tag === tag && item.installer === `OpenSquilla-${item.version}-win-x64.exe`
    && typeof item.sha256 === 'string' && /^[a-f0-9]{64}$/.test(item.sha256)
    && typeof item.bytes === 'number' && Number.isSafeInteger(item.bytes)
    && item.bytes > 0 && item.bytes <= MAX_INSTALLER_BYTES
}

function isForwardVersion(currentVersion: string, version: string): boolean {
  const current = parseOpenSquillaReleaseTag(currentVersion)
  const candidate = parseOpenSquillaReleaseTag(version)
  if (!current || !candidate) return false
  if (current.rc !== null) {
    return candidate.base === current.base && (candidate.rc === null || candidate.rc > current.rc)
  }
  if (candidate.rc !== null) return false
  const left = current.base.split('.').map(Number)
  const right = candidate.base.split('.').map(Number)
  const different = right.findIndex((value, index) => value !== left[index])
  return different >= 0 && right[different] > left[different]
}

export function createWindowsUpdateCacheDescriptor(
  candidate: Pick<DesktopUpdateCandidate, 'tag' | 'version' | 'installer'>,
  sha256: string,
  bytes: number,
): WindowsUpdateCacheDescriptor {
  const result: WindowsUpdateCacheDescriptor = {
    schemaVersion: 1, tag: candidate.tag, version: candidate.version,
    installer: candidate.installer, sha256: sha256.toLowerCase(), bytes,
  }
  if (!validDescriptor(result)) throw new Error('The Windows installer cache descriptor is invalid.')
  return result
}

export async function readWindowsUpdateCacheDescriptor(directory: string): Promise<WindowsUpdateCacheDescriptor | null> {
  try {
    const file = join(directory, WINDOWS_UPDATE_CACHE_DESCRIPTOR)
    const info = await lstat(file)
    if (!info.isFile() || info.isSymbolicLink() || info.size > 16 * 1024) return null
    const parsed: unknown = JSON.parse(await readFile(file, 'utf8'))
    return validDescriptor(parsed) ? createWindowsUpdateCacheDescriptor(parsed, parsed.sha256, parsed.bytes) : null
  } catch {
    return null
  }
}

export async function writeWindowsUpdateCacheDescriptor(directory: string, descriptor: WindowsUpdateCacheDescriptor): Promise<void> {
  if (!validDescriptor(descriptor)) throw new Error('The Windows installer cache descriptor is invalid.')
  await mkdir(directory, { recursive: true })
  const info = await lstat(directory)
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('The Windows installer cache directory is invalid.')
  const destination = join(directory, WINDOWS_UPDATE_CACHE_DESCRIPTOR)
  const temporary = `${destination}.${randomUUID()}.tmp`
  try {
    // Persist only canonical metadata; a caller-supplied absolute path can never
    // be serialized here and later promoted into an executable path.
    const safe = createWindowsUpdateCacheDescriptor(descriptor, descriptor.sha256, descriptor.bytes)
    await writeFile(temporary, `${JSON.stringify(safe)}\n`, { flag: 'wx', mode: 0o600 })
    await rename(temporary, destination)
  } finally {
    await rm(temporary, { force: true }).catch(() => {})
  }
}

export const loadWindowsUpdateCache = readWindowsUpdateCacheDescriptor

export async function saveWindowsUpdateCache(directory: string, descriptor: WindowsUpdateCacheDescriptor | null): Promise<void> {
  if (descriptor !== null) {
    await writeWindowsUpdateCacheDescriptor(directory, descriptor)
    return
  }
  try {
    const info = await lstat(directory)
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('The Windows installer cache directory is invalid.')
    await rm(join(directory, WINDOWS_UPDATE_CACHE_DESCRIPTOR), { force: true })
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }
}

export interface VerifyWindowsUpdateCacheOptions {
  expectedCandidate?: Pick<DesktopUpdateCandidate, 'tag' | 'version' | 'installer'>
  expectedSha256?: string
  verifySignature?: (path: string) => Promise<void>
}

export async function verifyCachedInstaller(
  directory: string,
  descriptor: unknown,
  currentVersion: string,
  options: VerifyWindowsUpdateCacheOptions = {},
): Promise<{ path: string; descriptor: WindowsUpdateCacheDescriptor; candidate: DesktopUpdateCandidate } | null> {
  if (!validDescriptor(descriptor) || !isForwardVersion(currentVersion, descriptor.version)) return null
  const expected = options.expectedCandidate
  if (expected && (expected.tag !== descriptor.tag || expected.version !== descriptor.version
    || expected.installer !== descriptor.installer)) return null
  if (options.expectedSha256 && options.expectedSha256.toLowerCase() !== descriptor.sha256) return null
  try {
    const directoryInfo = await lstat(directory)
    if (!directoryInfo.isDirectory() || directoryInfo.isSymbolicLink()) return null
    const root = await realpath(directory)
    const path = join(root, descriptor.installer)
    const before = await lstat(path)
    if (!before.isFile() || before.isSymbolicLink() || before.size !== descriptor.bytes) return null
    if (resolve(await realpath(path)) !== resolve(path)) return null
    const digest = createHash('sha256')
    for await (const chunk of createReadStream(path)) digest.update(chunk)
    if (digest.digest('hex') !== descriptor.sha256) return null
    await (options.verifySignature ?? verifyWindowsInstaller)(path)
    const after = await lstat(path)
    if (!after.isFile() || after.isSymbolicLink() || after.size !== before.size
      || after.ino !== before.ino || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) return null
    const parsed = parseOpenSquillaReleaseTag(descriptor.version)!
    const candidate: DesktopUpdateCandidate = {
      tag: descriptor.tag, version: descriptor.version, installer: descriptor.installer,
      baseVersion: parsed.base, prerelease: parsed.rc !== null,
      releaseUrl: `${UPDATE_GITHUB_RELEASE_PAGE_ROOT}/${descriptor.tag}`, feed: 'latest.yml',
    }
    return { path, descriptor: createWindowsUpdateCacheDescriptor(descriptor, descriptor.sha256, descriptor.bytes), candidate }
  } catch (error) {
    if (error instanceof WindowsUpdateSecurityError) throw error
    // Missing, stale or modified files are cache misses. Signature failures
    // remain typed so callers can distinguish a bad signature from unavailable
    // OS verification. Never delete or execute an unverified cache entry.
    return null
  }
}
