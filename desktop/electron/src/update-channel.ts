import { parseOpenSquillaReleaseTag, type ParsedReleaseTag } from './update-feed-resolver.js'

export const UPDATE_OSS_RELEASE_ROOT =
  'https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases'
export const UPDATE_GITHUB_RELEASE_ROOT =
  'https://github.com/opensquilla/opensquilla/releases/download'
export const UPDATE_GITHUB_RELEASE_PAGE_ROOT =
  'https://github.com/opensquilla/opensquilla/releases/tag'
export const UPDATE_GITHUB_RELEASES_API_URL =
  'https://api.github.com/repos/opensquilla/opensquilla/releases?per_page=100'

export type DesktopUpdatePlatform = 'darwin-arm64' | 'win32-x64'
export type DesktopUpdateSource = 'oss' | 'github'

export interface UpdateChannelPlatformEntry {
  feed: string
  installer: string
  archive?: string
}

export interface UpdateChannelManifest {
  schemaVersion: 1
  tag: string
  version: string
  baseVersion: string
  prerelease: boolean
  publishedAt: string
  releaseUrl: string
  sha256sums: string
  platforms: Record<DesktopUpdatePlatform, UpdateChannelPlatformEntry>
}

export interface DesktopUpdateCandidate {
  tag: string
  version: string
  baseVersion: string
  prerelease: boolean
  releaseUrl: string
  feed: string
  installer: string
  archive?: string
}

export class UpdateChannelError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'UpdateChannelError'
    this.code = code
  }
}

function invalid(message: string): never {
  throw new UpdateChannelError('manifest_invalid', message)
}

function safeFilename(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value || value === '.' || value === '..') {
    return invalid(`${field} must be a non-empty filename`)
  }
  if (value.includes('/') || value.includes('\\') || value.includes('\0')) {
    return invalid(`${field} must be a single filename`)
  }
  return value
}

function baseTuple(base: string): [number, number, number] {
  const parts = base.split('.')
  if (parts.length !== 3 || parts.some((part) => !/^\d+$/.test(part))) {
    return invalid(`invalid base version: ${base}`)
  }
  return parts.map(Number) as [number, number, number]
}

function compareBase(left: string, right: string): number {
  const a = baseTuple(left)
  const b = baseTuple(right)
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index]
  }
  return 0
}

function sameParsedVersion(left: ParsedReleaseTag, right: ParsedReleaseTag): boolean {
  return left.base === right.base && left.rc === right.rc
}

function canonicalTag(version: ParsedReleaseTag): string {
  return version.rc === null ? `v${version.base}` : `v${version.base}rc${version.rc}`
}

function canonicalAppVersion(version: ParsedReleaseTag): string {
  return version.rc === null ? version.base : `${version.base}-rc${version.rc}`
}

function validRfc3339(value: string): boolean {
  // GitHub emits UTC timestamps, but accept a standards-compliant numeric
  // offset too. Date.parse alone also accepts many non-RFC3339 date strings.
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))$/.exec(value)
  if (!match || Number.isNaN(Date.parse(value))) return false
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, offsetHourText, offsetMinuteText] = match
  const year = Number(yearText)
  const month = Number(monthText)
  const day = Number(dayText)
  const hour = Number(hourText)
  const minute = Number(minuteText)
  const second = Number(secondText)
  const offsetHour = Number(offsetHourText ?? 0)
  const offsetMinute = Number(offsetMinuteText ?? 0)
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate()
  return year >= 1
    && month >= 1 && month <= 12
    && day >= 1 && day <= daysInMonth
    && hour <= 23 && minute <= 59 && second <= 59
    && offsetHour <= 23 && offsetMinute <= 59
}

export function updateChannelPathForVersion(currentVersion: string): string | null {
  const parsed = parseOpenSquillaReleaseTag(currentVersion)
  if (!parsed) return null
  return parsed.rc === null ? 'stable.json' : `preview/${parsed.base}.json`
}

export function updateChannelManifestUrl(currentVersion: string, root = UPDATE_OSS_RELEASE_ROOT): string | null {
  const path = updateChannelPathForVersion(currentVersion)
  if (!path) return null
  return `${root.replace(/\/+$/, '')}/channels/${path}`
}

function requiredReleaseAssets(version: ParsedReleaseTag): string[] {
  const appVersion = canonicalAppVersion(version)
  return [
    'SHA256SUMS',
    'latest-mac.yml',
    'latest.yml',
    `OpenSquilla-${appVersion}-mac-arm64.zip`,
    `OpenSquilla-${appVersion}-mac-arm64.dmg`,
    `OpenSquilla-${appVersion}-win-x64.exe`,
  ]
}

function releaseOutranks(candidate: ParsedReleaseTag, incumbent: ParsedReleaseTag): boolean {
  const byBase = compareBase(candidate.base, incumbent.base)
  if (byBase !== 0) return byBase > 0
  if (candidate.rc === null) return incumbent.rc !== null
  if (incumbent.rc === null) return false
  return candidate.rc > incumbent.rc
}

// Build the same channel manifest the release mirror publishes, but from the
// GitHub release inventory (the releases API listing). This is the second
// discovery source: when the mirrored channel manifest is unreachable, the
// channel head is recomputed from GitHub metadata and then flows through the
// exact same manifest validation as a mirrored manifest. Returns null when the
// current version has no channel or no published release is eligible for it.
export function updateChannelManifestFromReleaseInventory(
  currentVersion: string,
  inventory: unknown,
): UpdateChannelManifest | null {
  const current = parseOpenSquillaReleaseTag(currentVersion)
  if (!current) return null
  if (!Array.isArray(inventory)) {
    throw new UpdateChannelError('manifest_invalid', 'The GitHub release inventory must be an array.')
  }
  let best: { parsed: ParsedReleaseTag; tag: string; publishedAt: string } | null = null
  for (const raw of inventory) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    const release = raw as Record<string, unknown>
    if (release.draft === true) continue
    const tag = typeof release.tag_name === 'string' ? release.tag_name.trim() : ''
    const parsed = parseOpenSquillaReleaseTag(tag)
    // Only canonically-spelled tags participate: the manifest contract and the
    // release download URLs must agree on the exact tag text.
    if (!parsed || tag !== canonicalTag(parsed)) continue
    if (typeof release.prerelease === 'boolean' && release.prerelease !== (parsed.rc !== null)) {
      continue
    }
    if (current.rc === null) {
      // The stable channel only ever advances to final releases.
      if (parsed.rc !== null) continue
    } else if (parsed.base !== current.base) {
      // The preview channel tracks a single release line.
      continue
    }
    const publishedAt = typeof release.published_at === 'string' ? release.published_at.trim() : ''
    if (!publishedAt || !validRfc3339(publishedAt)) continue
    const assetNames = new Set<string>()
    for (const asset of Array.isArray(release.assets) ? release.assets : []) {
      const name = (asset as { name?: unknown } | null)?.name
      if (typeof name === 'string') assetNames.add(name)
    }
    if (requiredReleaseAssets(parsed).some((name) => !assetNames.has(name))) continue
    if (best && !releaseOutranks(parsed, best.parsed)) continue
    best = { parsed, tag, publishedAt }
  }
  if (!best) return null
  const version = canonicalAppVersion(best.parsed)
  return validateUpdateChannelManifest({
    schemaVersion: 1,
    tag: best.tag,
    version,
    baseVersion: best.parsed.base,
    prerelease: best.parsed.rc !== null,
    publishedAt: best.publishedAt,
    releaseUrl: `${UPDATE_GITHUB_RELEASE_PAGE_ROOT}/${best.tag}`,
    sha256sums: 'SHA256SUMS',
    platforms: {
      'darwin-arm64': {
        feed: 'latest-mac.yml',
        archive: `OpenSquilla-${version}-mac-arm64.zip`,
        installer: `OpenSquilla-${version}-mac-arm64.dmg`,
      },
      'win32-x64': {
        feed: 'latest.yml',
        installer: `OpenSquilla-${version}-win-x64.exe`,
      },
    },
  })
}

export function validateUpdateChannelManifest(payload: unknown): UpdateChannelManifest {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return invalid('channel manifest must be an object')
  }
  const raw = payload as Record<string, unknown>
  if (raw.schemaVersion !== 1) return invalid('unsupported channel manifest schemaVersion')

  const tag = typeof raw.tag === 'string' ? raw.tag.trim() : ''
  const version = typeof raw.version === 'string' ? raw.version.trim() : ''
  const baseVersion = typeof raw.baseVersion === 'string' ? raw.baseVersion.trim() : ''
  const parsedTag = parseOpenSquillaReleaseTag(tag)
  const parsedVersion = parseOpenSquillaReleaseTag(version)
  if (!parsedTag || !parsedVersion || !sameParsedVersion(parsedTag, parsedVersion)) {
    return invalid('channel manifest tag and version disagree')
  }
  if (tag !== canonicalTag(parsedTag) || version !== canonicalAppVersion(parsedTag)) {
    return invalid('channel manifest tag and version are not canonical')
  }
  if (baseVersion !== parsedTag.base) return invalid('channel manifest baseVersion disagrees with tag')
  if (raw.prerelease !== (parsedTag.rc !== null)) {
    return invalid('channel manifest prerelease disagrees with tag')
  }

  const publishedAt = typeof raw.publishedAt === 'string' ? raw.publishedAt.trim() : ''
  if (!publishedAt || !validRfc3339(publishedAt)) {
    return invalid('channel manifest publishedAt is invalid')
  }
  const releaseUrl = `${UPDATE_GITHUB_RELEASE_PAGE_ROOT}/${tag}`
  if (raw.releaseUrl !== releaseUrl) return invalid('channel manifest releaseUrl is not canonical')
  const sha256sums = safeFilename(raw.sha256sums, 'sha256sums')
  if (sha256sums !== 'SHA256SUMS') return invalid('channel manifest sha256sums is invalid')

  if (!raw.platforms || typeof raw.platforms !== 'object' || Array.isArray(raw.platforms)) {
    return invalid('channel manifest platforms must be an object')
  }
  const platforms = raw.platforms as Record<string, unknown>
  const parsedPlatforms = {} as Record<DesktopUpdatePlatform, UpdateChannelPlatformEntry>
  for (const platform of ['darwin-arm64', 'win32-x64'] as const) {
    const entry = platforms[platform]
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      return invalid(`channel manifest is missing ${platform}`)
    }
    const values = entry as Record<string, unknown>
    parsedPlatforms[platform] = {
      feed: safeFilename(values.feed, `${platform}.feed`),
      installer: safeFilename(values.installer, `${platform}.installer`),
      ...(values.archive === undefined
        ? {}
        : { archive: safeFilename(values.archive, `${platform}.archive`) }),
    }
  }


  const expectedMacArchive = `OpenSquilla-${version}-mac-arm64.zip`
  const expectedMacInstaller = `OpenSquilla-${version}-mac-arm64.dmg`
  const expectedWindowsInstaller = `OpenSquilla-${version}-win-x64.exe`
  if (
    parsedPlatforms['darwin-arm64'].feed !== 'latest-mac.yml'
    || parsedPlatforms['darwin-arm64'].archive !== expectedMacArchive
    || parsedPlatforms['darwin-arm64'].installer !== expectedMacInstaller
    || parsedPlatforms['win32-x64'].feed !== 'latest.yml'
    || parsedPlatforms['win32-x64'].installer !== expectedWindowsInstaller
  ) {
    return invalid('channel manifest platform assets do not match the release version')
  }

  return {
    schemaVersion: 1,
    tag,
    version,
    baseVersion,
    prerelease: parsedTag.rc !== null,
    publishedAt,
    releaseUrl,
    sha256sums,
    platforms: parsedPlatforms,
  }
}

function candidateIsNewer(current: ParsedReleaseTag, candidate: ParsedReleaseTag): boolean {
  if (current.rc !== null) {
    if (candidate.base !== current.base) return false
    return candidate.rc === null || candidate.rc > current.rc
  }
  return candidate.rc === null && compareBase(candidate.base, current.base) > 0
}

export function candidateFromUpdateChannel(
  currentVersion: string,
  payload: unknown,
  platform: DesktopUpdatePlatform,
): DesktopUpdateCandidate | null {
  const current = parseOpenSquillaReleaseTag(currentVersion)
  if (!current) throw new UpdateChannelError('current_version_invalid', 'current app version is unsupported')
  const manifest = validateUpdateChannelManifest(payload)
  const candidate = parseOpenSquillaReleaseTag(manifest.version)
  if (!candidate) return invalid('validated manifest version could not be parsed')
  if (!candidateIsNewer(current, candidate)) return null
  const entry = manifest.platforms[platform]
  return {
    tag: manifest.tag,
    version: manifest.version,
    baseVersion: manifest.baseVersion,
    prerelease: manifest.prerelease,
    releaseUrl: manifest.releaseUrl,
    feed: entry.feed,
    installer: entry.installer,
    archive: entry.archive,
  }
}

export function updateFeedBaseUrl(candidate: DesktopUpdateCandidate, source: DesktopUpdateSource): string {
  const root = source === 'oss' ? UPDATE_OSS_RELEASE_ROOT : UPDATE_GITHUB_RELEASE_ROOT
  return `${root}/${candidate.tag}`
}

export function updateAssetUrl(
  candidate: DesktopUpdateCandidate,
  source: DesktopUpdateSource,
  asset = candidate.installer,
): string {
  return `${updateFeedBaseUrl(candidate, source)}/${encodeURIComponent(asset)}`
}

export function orderedUpdateSources(
  localeTags: readonly unknown[],
  lastSuccessful: DesktopUpdateSource | null = null,
  override: string | undefined = undefined,
): DesktopUpdateSource[] {
  const normalizedOverride = String(override ?? '').trim().toLowerCase()
  if (normalizedOverride === 'oss' || normalizedOverride === 'china') return ['oss', 'github']
  if (normalizedOverride === 'github' || normalizedOverride === 'global') return ['github', 'oss']

  if (lastSuccessful) return lastSuccessful === 'oss' ? ['oss', 'github'] : ['github', 'oss']

  let mainlandHint = false
  for (const raw of localeTags) {
    if (typeof raw !== 'string') continue
    try {
      const locale = new Intl.Locale(raw.trim().replaceAll('_', '-'))
      if (locale.region?.toUpperCase() === 'CN') {
        mainlandHint = true
        break
      }
      // A language-only Simplified Chinese tag is a weak hint. Explicit
      // non-mainland regions (for example zh-SG) stay on the global order.
      if (
        locale.language.toLowerCase() === 'zh'
        && !locale.region
        && locale.script?.toLowerCase() !== 'hant'
      ) {
        mainlandHint = true
      }
    } catch {
      // Ignore malformed OS locale entries.
    }
  }
  return mainlandHint ? ['oss', 'github'] : ['github', 'oss']
}
