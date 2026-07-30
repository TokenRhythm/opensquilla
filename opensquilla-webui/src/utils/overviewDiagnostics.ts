// Pure helpers behind the Overview diagnostics actions (copy-JSON, "diagnose
// with agent", finding→settings deep links, provider latency line). Kept out
// of the component so privacy-sensitive normalization and the conservative
// surface→settings map stay unit-testable without mounting the view.

export interface DiagnosticFindingLike {
  id?: string
  surface?: string
  severity?: string
  readinessImpact?: string
  title?: string
  detail?: string
  evidence?: Record<string, unknown>
  fixSteps?: Array<{
    label?: string
    command?: string
    detail?: string
  }>
}

export interface FindingSettingsLink {
  path: string
  hash?: string
  query?: Record<string, string>
}

export interface FindingSettingsContext {
  isDesktop: boolean
  ownsGatewayConnection: boolean
}

export interface AgentDiagnosisContextInput {
  platform: 'web' | 'desktop'
  hasTerminalWorkflow: boolean
  ownsGateway: boolean
  ownsGatewayConnection: boolean
}

export interface DiagnosticReportLike {
  status?: string
  ready?: boolean
  summary?: string
  counts?: Record<string, number>
  impactCounts?: Record<string, number>
  findings?: DiagnosticFindingLike[]
}

const LEGACY_MIGRATION_FINDING_ID = 'migration.legacy_home_detected'

/**
 * New gateways no longer publish optional legacy-home discovery through
 * Doctor. Filter the old finding client-side as well so a new UI connected to
 * an older gateway keeps migration strictly inside Data & backups.
 */
export function withoutLegacyMigrationFinding<T extends DiagnosticReportLike>(report: T): T {
  const findings = Array.isArray(report.findings) ? report.findings : []
  const removed = findings.filter(finding => finding?.id === LEGACY_MIGRATION_FINDING_ID)
  if (removed.length === 0) return report

  const filtered = findings.filter(finding => finding?.id !== LEGACY_MIGRATION_FINDING_ID)
  const counts: Record<string, number> = { error: 0, warn: 0, info: 0, ok: 0 }
  const impactCounts: Record<string, number> = {
    blocks_ready: 0,
    degrades: 0,
    optional: 0,
    none: 0,
  }
  const defaultImpact: Record<string, string> = {
    error: 'blocks_ready',
    warn: 'degrades',
    info: 'optional',
    ok: 'none',
  }
  for (const finding of filtered) {
    const severity = String(finding.severity || '')
    const impact = String(finding.readinessImpact || defaultImpact[severity] || '')
    if (severity in counts) counts[severity] += 1
    if (impact in impactCounts) impactCounts[impact] += 1
  }
  const blocks = impactCounts.blocks_ready
  const degrades = impactCounts.degrades
  const optional = impactCounts.optional
  const status = blocks > 0 ? 'action_required' : degrades > 0 ? 'degraded' : 'ready'
  const summaryParts: string[] = []
  if (blocks > 0) summaryParts.push(`${blocks} ${blocks === 1 ? 'action' : 'actions'} required`)
  if (degrades > 0) {
    const degraded = `${degrades} degraded ${degrades === 1 ? 'check' : 'checks'}`
    summaryParts.push(blocks > 0 ? degraded : `Ready, ${degraded}`)
  }
  const summary = summaryParts.length > 0
    ? summaryParts.join(', ')
    : optional > 0
      ? `Ready, ${optional} optional setup ${optional === 1 ? 'item' : 'items'}`
      : 'Ready'
  return {
    ...report,
    status,
    ready: blocks === 0,
    summary,
    findings: filtered,
    counts,
    impactCounts,
  }
}

// A home directory embedded in serialized diagnostics leaks the local account
// name. Collapse `/Users/<name>/` (macOS) and `/home/<name>/` (Linux) to `~/`.
// The username segment excludes `/`, `"` and `\` so the match never crosses a
// JSON string boundary or an escape sequence.
const HOME_PATH_RE = /\/(?:Users|home)\/[^/"\\]+\//g

export function normalizeHomePaths(text: string): string {
  return text.replace(HOME_PATH_RE, '~/')
}

// Minimal XML escaping (& < >) so a report wrapped in an <untrusted> envelope
// cannot close the tag or smuggle markup into the prompt.
export function xmlEscape(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// The in-app "diagnose with agent" hand-off needs a working provider: when a
// provider finding blocks readiness the agent turn itself would fail, so the
// action must be hidden instead of sending the operator into a dead chat.
export function providerBlocksAgent(
  findings: readonly DiagnosticFindingLike[] | undefined | null,
): boolean {
  return (findings || []).some(finding =>
    String(finding?.surface || '') === 'provider'
    && (
      String(finding?.readinessImpact || '') === 'blocks_ready'
      || String(finding?.severity || '') === 'error'
    ),
  )
}

// Only providerIds shaped like registry slugs are trusted into a URL hash.
const PROVIDER_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

// Conservative surface→settings map: only surfaces with an unambiguous
// settings section get a deep link; everything else renders no link at all.
export function settingsLinkForFinding(
  finding: DiagnosticFindingLike | undefined | null,
  _context?: FindingSettingsContext,
): FindingSettingsLink | null {
  const surface = String(finding?.surface || '')
  if (surface === 'provider') {
    const providerId = finding?.evidence?.providerId
    if (typeof providerId === 'string' && PROVIDER_ID_RE.test(providerId)) {
      return { path: '/settings/provider', hash: `#provider-${providerId}` }
    }
    return { path: '/settings/provider' }
  }
  if (surface === 'channels') {
    // Channel setup lives on the /channels workspace now. Runtime findings
    // carry the channel name in evidence once the backend populates it; a
    // nameless finding degrades to the workspace itself. Query params have no
    // reserved names, so even a channel literally named "new" deep-links.
    const name = finding?.evidence?.channelName ?? finding?.evidence?.channel
    if (typeof name === 'string' && name.trim()) {
      return {
        path: '/channels',
        query: { channel: name.trim(), tab: 'configuration', edit: '1' },
      }
    }
    return { path: '/channels' }
  }
  if (surface === 'router' || surface === 'squilla_router') {
    return { path: '/settings/modelStrategy' }
  }
  if (surface === 'image_generation' || surface === 'search' || surface === 'memory_embedding') {
    return { path: '/settings/capabilities' }
  }
  return null
}

function settingsRoute(link: FindingSettingsLink): string {
  const query = link.query
    ? `?${new URLSearchParams(link.query).toString()}`
    : ''
  return `${link.path}${query}${link.hash || ''}`
}

// The Desktop agent hand-off is explanatory only. Recursively remove every
// command field from its model-visible report copy while leaving the original
// Doctor report untouched and marking where the folded Doctor page retains an
// advanced CLI fallback.
function stripCommandFields(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripCommandFields)
  if (!value || typeof value !== 'object') return value

  const output: Record<string, unknown> = {}
  let strippedCommand = false
  for (const [key, entry] of Object.entries(value)) {
    if (key === 'command') {
      strippedCommand = true
      continue
    }
    output[key] = stripCommandFields(entry)
  }
  if (strippedCommand) output.advancedCliAvailable = true
  return output
}

/**
 * Build the privacy-safe, surface-aware payload passed to a diagnostic agent.
 * It does not mutate the public doctor report or add any execution capability.
 */
export function buildAgentDiagnosisHandoff(
  report: DiagnosticReportLike,
  input: AgentDiagnosisContextInput,
) {
  const visibleReport = withoutLegacyMigrationFinding(report)
  const ownsCurrentGateway = input.ownsGateway && input.ownsGatewayConnection
  const settingsContext: FindingSettingsContext = {
    isDesktop: input.platform === 'desktop',
    ownsGatewayConnection: ownsCurrentGateway,
  }
  const settingsRemediations = (visibleReport.findings || []).flatMap((finding) => {
    const link = settingsLinkForFinding(finding, settingsContext)
    if (!link) return []
    return [{
      findingId: String(finding.id || finding.surface || 'unknown'),
      route: settingsRoute(link),
    }]
  })
  const applicableSettingsRoutes = Array.from(new Set(
    settingsRemediations.map(item => item.route),
  ))

  return {
    clientContext: {
      platform: input.platform,
      hasTerminalWorkflow: input.hasTerminalWorkflow,
      ownsGateway: ownsCurrentGateway,
      connectionScope: ownsCurrentGateway
        ? 'local_owned'
        : input.platform === 'desktop' ? 'remote' : 'external',
      applicableSettingsRoutes,
      settingsRemediations,
      guidance: {
        preferInAppSettings: !input.hasTerminalWorkflow,
        allowBareCliCommands: input.hasTerminalWorkflow,
        gatewayLifecycle: ownsCurrentGateway ? 'managed_by_desktop_app' : 'operator_managed',
        remoteGatewayActions: ownsCurrentGateway ? 'not_applicable' : 'handle_on_gateway_host',
        migrationMode: 'backup_then_replace_no_merge',
        optionalMigrationIsFailure: false,
        migrationFindingGuaranteedToClear: false,
        advancedCliFallback: input.hasTerminalWorkflow
          ? 'included_in_report'
          : 'available_on_doctor_page',
      },
    },
    report: input.hasTerminalWorkflow
      ? visibleReport
      : stripCommandFields(visibleReport) as DiagnosticReportLike,
  }
}

function finiteNonNegative(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function formatTtft(ms: number): string {
  if (ms >= 1000) {
    const seconds = ms / 1000
    return `${seconds >= 10 ? Math.round(seconds) : Number(seconds.toFixed(1))}s`
  }
  return `${Math.round(ms)}ms`
}

// Compact mono latency readout, e.g. 'p50 380ms · p95 1.2s · 87 samples/60min'.
// Every field is optional: backends that predate TTFT stats send no latency
// object at all, and low-sample windows null out individual percentiles —
// returns null when nothing is renderable so callers can skip the line.
export function formatLatencyLine(latency: unknown): string | null {
  if (!latency || typeof latency !== 'object' || Array.isArray(latency)) return null
  const record = latency as Record<string, unknown>
  const parts: string[] = []
  const p50 = finiteNonNegative(record.p50TtftMs)
  const p95 = finiteNonNegative(record.p95TtftMs)
  if (p50 != null) parts.push(`p50 ${formatTtft(p50)}`)
  if (p95 != null) parts.push(`p95 ${formatTtft(p95)}`)
  const samples = finiteNonNegative(record.samples)
  if (samples != null) {
    const windowMinutes = finiteNonNegative(record.windowMinutes)
    parts.push(windowMinutes != null ? `${samples} samples/${windowMinutes}min` : `${samples} samples`)
  }
  return parts.length ? parts.join(' · ') : null
}
