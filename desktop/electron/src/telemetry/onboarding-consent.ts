import { CURRENT_NOTICE_VERSION_BY_SCOPE } from './contracts.js'

export interface DesktopScopeConsent {
  enabled: boolean | null
  noticeVersion: string | null
  consentedAtUtc: string | null
}

export interface DesktopTelemetryConsent {
  reliability: DesktopScopeConsent
  growth: DesktopScopeConsent
}

export interface DesktopTelemetryConsentPayload {
  reliabilityDiagnosticsEnabled?: unknown
  productAnalyticsEnabled?: unknown
}

const EMPTY_SCOPE = (): DesktopScopeConsent => ({
  enabled: null,
  noticeVersion: null,
  consentedAtUtc: null,
})

const PRIVACY_FIELDS = {
  reliability: {
    enabled: 'reliability_diagnostics_enabled',
    notice: 'reliability_notice_version',
    timestamp: 'reliability_consented_at_utc',
  },
  growth: {
    enabled: 'product_analytics_enabled',
    notice: 'product_analytics_notice_version',
    timestamp: 'product_analytics_consented_at_utc',
  },
} as const

function unquoteTomlString(value: string): string | null {
  const candidate = value.trim()
  if (candidate.startsWith('"') && candidate.endsWith('"')) {
    try {
      const parsed: unknown = JSON.parse(candidate)
      return typeof parsed === 'string' ? parsed : null
    } catch {
      return null
    }
  }
  if (candidate.startsWith("'") && candidate.endsWith("'")) {
    return candidate.slice(1, -1)
  }
  return null
}

function privacyAssignments(raw: string | null): Map<string, string> {
  const assignments = new Map<string, string>()
  if (raw === null) return assignments
  let inPrivacy = false
  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim()
    const section = line.match(/^\[\s*([^\]]+?)\s*\](?:\s*#.*)?$/)
    if (section) {
      inPrivacy = section[1] === 'privacy'
      continue
    }
    if (!inPrivacy || !line || line.startsWith('#')) continue
    const assignment = line.match(/^([A-Za-z0-9_-]+)\s*=\s*(.*)$/)
    if (assignment) {
      assignments.set(assignment[1] ?? '', String(assignment[2] ?? '').split('#', 1)[0]?.trim() ?? '')
    }
  }
  return assignments
}

function parsedScope(
  assignments: ReadonlyMap<string, string>,
  fields: (typeof PRIVACY_FIELDS)[keyof typeof PRIVACY_FIELDS],
): DesktopScopeConsent {
  const enabledValue = assignments.get(fields.enabled)?.trim().toLowerCase()
  const enabled = enabledValue === 'true' ? true : enabledValue === 'false' ? false : null
  if (enabled !== true) {
    // An unset or explicit decline never retains metadata that could be mistaken
    // for a grant by an older reader.
    return { enabled, noticeVersion: null, consentedAtUtc: null }
  }
  return {
    enabled,
    noticeVersion: unquoteTomlString(assignments.get(fields.notice) ?? ''),
    consentedAtUtc: unquoteTomlString(assignments.get(fields.timestamp) ?? ''),
  }
}

/** Parse only the six Desktop-owned scoped consent fields from [privacy]. */
export function parseDesktopTelemetryConsent(raw: string | null): DesktopTelemetryConsent {
  const assignments = privacyAssignments(raw)
  return {
    reliability: parsedScope(assignments, PRIVACY_FIELDS.reliability),
    growth: parsedScope(assignments, PRIVACY_FIELDS.growth),
  }
}

export function parseLegacyNetworkObservabilityDisabled(raw: string | null): boolean | null {
  const value = privacyAssignments(raw).get('disable_network_observability')?.trim().toLowerCase()
  if (value === 'true') return true
  if (value === 'false') return false
  return value === undefined ? null : true
}

function explicitChoice(payload: DesktopTelemetryConsentPayload, key: keyof DesktopTelemetryConsentPayload): boolean | null {
  if (!Object.prototype.hasOwnProperty.call(payload, key)) return null
  const value = payload[key]
  if (typeof value !== 'boolean') throw new TypeError(`${key} must be a boolean.`)
  return value
}

function scopeFromChoice(
  enabled: boolean,
  scope: 'reliability' | 'growth',
  nowUtc: string,
): DesktopScopeConsent {
  return enabled
    ? {
        enabled: true,
        noticeVersion: CURRENT_NOTICE_VERSION_BY_SCOPE[scope],
        consentedAtUtc: nowUtc,
      }
    : { enabled: false, noticeVersion: null, consentedAtUtc: null }
}

/**
 * Apply only explicit choices. Missing properties preserve the persisted state;
 * a fresh onboarding must call requireExplicitOnboardingConsent first.
 */
export function applyDesktopTelemetryConsentPayload(
  persisted: DesktopTelemetryConsent,
  payload: DesktopTelemetryConsentPayload,
  nowUtc: string,
): DesktopTelemetryConsent {
  const reliability = explicitChoice(payload, 'reliabilityDiagnosticsEnabled')
  const growth = explicitChoice(payload, 'productAnalyticsEnabled')
  return {
    reliability: reliability === null
      ? persisted.reliability
      : scopeFromChoice(reliability, 'reliability', nowUtc),
    growth: growth === null ? persisted.growth : scopeFromChoice(growth, 'growth', nowUtc),
  }
}

export function requireExplicitOnboardingConsent(payload: DesktopTelemetryConsentPayload): void {
  if (
    typeof payload.reliabilityDiagnosticsEnabled !== 'boolean'
    || typeof payload.productAnalyticsEnabled !== 'boolean'
  ) {
    throw new TypeError('Choose whether to enable both telemetry categories before continuing.')
  }
}

function tomlString(value: string): string {
  return JSON.stringify(value)
}

function scopeTomlLines(
  state: DesktopScopeConsent,
  fields: (typeof PRIVACY_FIELDS)[keyof typeof PRIVACY_FIELDS],
): string[] {
  if (state.enabled === null) return []
  if (state.enabled === false) return [`${fields.enabled} = false`]
  return [
    `${fields.enabled} = true`,
    ...(state.noticeVersion === null ? [] : [`${fields.notice} = ${tomlString(state.noticeVersion)}`]),
    ...(state.consentedAtUtc === null ? [] : [`${fields.timestamp} = ${tomlString(state.consentedAtUtc)}`]),
  ]
}

export function desktopPrivacyTomlLines(
  legacyDisabled: boolean,
  consent: DesktopTelemetryConsent,
  includeLegacy: boolean,
): string[] {
  const scopedLines = [
    ...scopeTomlLines(consent.reliability, PRIVACY_FIELDS.reliability),
    ...scopeTomlLines(consent.growth, PRIVACY_FIELDS.growth),
  ]
  if (!includeLegacy && scopedLines.length === 0) return []
  return [
    '',
    '[privacy]',
    ...(includeLegacy ? [`disable_network_observability = ${legacyDisabled ? 'true' : 'false'}`] : []),
    ...scopedLines,
  ]
}

const SCOPED_FIELD_NAMES: ReadonlySet<string> = new Set(
  Object.values(PRIVACY_FIELDS).flatMap((fields) => [fields.enabled, fields.notice, fields.timestamp]),
)

/** Patch only the six consent keys while preserving an imported config verbatim otherwise. */
export function replaceDesktopTelemetryConsentInPrivacy(
  raw: string,
  consent: DesktopTelemetryConsent,
): string {
  const eol = raw.includes('\r\n') ? '\r\n' : '\n'
  const hadTrailingNewline = /\r?\n$/.test(raw)
  const lines = raw.split(/\r?\n/)
  if (hadTrailingNewline) lines.pop()
  let privacyStart = -1
  let privacyEnd = lines.length
  for (let index = 0; index < lines.length; index += 1) {
    const section = lines[index]?.trim().match(/^\[\s*([^\]]+?)\s*\](?:\s*#.*)?$/)
    if (!section) continue
    if (privacyStart >= 0) {
      privacyEnd = index
      break
    }
    if (section[1] === 'privacy') privacyStart = index
  }
  const scopedLines = [
    ...scopeTomlLines(consent.reliability, PRIVACY_FIELDS.reliability),
    ...scopeTomlLines(consent.growth, PRIVACY_FIELDS.growth),
  ]
  if (privacyStart < 0) {
    if (lines.length && lines[lines.length - 1]?.trim() !== '') lines.push('')
    lines.push('[privacy]', ...scopedLines)
  } else {
    const keptPrivacyLines = lines.slice(privacyStart + 1, privacyEnd).filter((line) => {
      const assignment = line.match(/^\s*([A-Za-z0-9_-]+)\s*=/)
      return !assignment || !SCOPED_FIELD_NAMES.has(assignment[1] ?? '')
    })
    while (keptPrivacyLines.length && keptPrivacyLines[keptPrivacyLines.length - 1]?.trim() === '') {
      keptPrivacyLines.pop()
    }
    lines.splice(
      privacyStart + 1,
      privacyEnd - privacyStart - 1,
      ...keptPrivacyLines,
      ...scopedLines,
      ...(privacyEnd < lines.length ? [''] : []),
    )
  }
  return `${lines.join(eol)}${hadTrailingNewline ? eol : ''}`
}

export function emptyDesktopTelemetryConsent(): DesktopTelemetryConsent {
  return { reliability: EMPTY_SCOPE(), growth: EMPTY_SCOPE() }
}
