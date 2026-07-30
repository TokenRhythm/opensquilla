// Shared channel presentation facts: the row types the gateway's
// channels.status returns plus the pure formatting helpers the dashboard
// cards and the drill-in page both render from. Extracted from ChannelsView
// so the card grid and the full-page detail cannot drift on how a channel's
// identity, transport, uptime, or delivery health is derived.

export interface CapabilityEvidence {
  declared?: boolean
  implemented?: boolean
  effective?: boolean
  evidence_kind?: string
  methods?: string[]
  proof_status?: string
}

export interface Channel {
  name?: string
  id?: string
  type?: string
  status?: string
  connected?: boolean
  connected_since?: string | number | null
  restart_attempts?: number
  pendingPairings?: number
  bot_user_id?: string | null
  enabled?: boolean
  configured?: boolean
  capabilities?: string[]
  capability_profile?: {
    transports?: string[]
    maturity?: string
    evidence?: Record<string, CapabilityEvidence>
  } | null
  diagnostics?: Record<string, unknown>
  [key: string]: unknown
}

export interface ProbeResult {
  status: string
  connected: boolean
  latencyMs?: number | null
  detail?: string
  result?: Record<string, unknown>
}

export function channelKey(ch: Channel): string {
  return String(ch.name || ch.id || ch.type || 'unknown')
}

// Curated per-type labels and a static transport, so a not-yet-loaded channel
// (no runtime capability_profile) still reads with the platform's real name
// and transport instead of a title-cased type slug.
const CURATED_LABELS: Record<string, string> = {
  slack: 'Slack', telegram: 'Telegram', discord: 'Discord', feishu: 'Feishu / Lark',
  dingtalk: 'DingTalk', wecom: 'WeCom', qq: 'QQ Bot', matrix: 'Matrix', msteams: 'Microsoft Teams',
}
const STATIC_TRANSPORT: Record<string, string> = {
  slack: 'Mixed', telegram: 'Polling', discord: 'WebSocket', feishu: 'Mixed',
  dingtalk: 'WebSocket', wecom: 'Mixed', qq: 'WebSocket', matrix: 'HTTP sync', msteams: 'Webhook',
}

export function humanize(value: string): string {
  return value.replace(/[_-]+/g, ' ').replace(/\b\w/g, char => char.toUpperCase())
}

export function providerLabel(type?: string, unknownLabel = 'Unknown'): string {
  const key = String(type || '').toLowerCase()
  return CURATED_LABELS[key] || humanize(type || unknownLabel)
}

export function transportLabel(ch: Channel, notReported = ''): string {
  const live = (ch.capability_profile?.transports || []).map(humanize).join(' / ')
  if (live) return live
  return STATIC_TRANSPORT[String(ch.type || '').toLowerCase()] || notReported
}

export function formatSince(
  since?: string | number | null,
  locale?: string,
): string {
  if (!since) return '—'
  const date = new Date(since)
  return Number.isNaN(date.getTime()) ? String(since) : date.toLocaleString(locale)
}

/**
 * Compact uptime for the card facts row: "4d 2h", "11h", "23m", "<1m".
 * Unit letters are deliberately latin-universal (matches tabular figures).
 */
export function formatConnectedDuration(since?: string | number | null, now = Date.now()): string {
  if (!since) return '—'
  const start = new Date(since).getTime()
  if (Number.isNaN(start)) return '—'
  const totalMinutes = Math.floor((now - start) / 60000)
  if (totalMinutes < 1) return '<1m'
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`
  if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`
  return `${minutes}m`
}

export function diagnostics(ch: Channel): Record<string, unknown> {
  return ch.diagnostics && typeof ch.diagnostics === 'object' ? ch.diagnostics : {}
}

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

export function delivery(ch: Channel): Record<string, unknown> {
  const value = diagnostics(ch).delivery
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

export function deliveryCount(ch: Channel, section: string, state: string): number {
  return Number(record(record(delivery(ch)[section])[state]).count || 0)
}

/** Total inbound rows across ALL ledger states. The ingress lifecycle is
 *  accepted → processing → completed, and completed rows persist — so "has
 *  this channel EVER received an event" must sum every state, not sample the
 *  transient in-flight ones. */
export function ingressTotal(ch: Channel): number {
  return Object.values(record(delivery(ch).ingress))
    .reduce<number>((total, row) => total + Number(record(row).count || 0), 0)
}

export function lastError(ch: Channel): string {
  const value = diagnostics(ch).last_error
  if (!value || typeof value !== 'object') return ''
  return String(record(value).message || record(value).error_class || '')
}

export function maturityKey(ch: Channel): string {
  return String(ch.capability_profile?.maturity || 'unrated').replace(/^[A-Z]+-/, '').toLowerCase()
}

export const MATURITY_KEYS = new Set(['experimental', 'stable', 'shipping', 'unrated'])

export function capabilityRows(ch: Channel): Array<CapabilityEvidence & { name: string }> {
  const evidence = ch.capability_profile?.evidence || {}
  return Object.entries(evidence)
    .map(([name, value]) => ({ name, ...value }))
    .sort((a, b) =>
      Number(Boolean(b.effective)) - Number(Boolean(a.effective)) || a.name.localeCompare(b.name))
}

export interface PlatformCapabilityRow {
  category: string
  status?: string
  notes?: string[]
}

export function platformRows(
  ch: Channel,
): Array<{ category: string; tone: string; status: string; notes: string }> {
  const caps = (ch.platform_manifest as { capabilities?: Record<string, PlatformCapabilityRow> } | null)
    ?.capabilities
  if (!caps || typeof caps !== 'object') return []
  return Object.entries(caps).map(([category, row]) => {
    const status = String(row?.status || 'unsupported')
    const tone = status === 'supported' ? 'ok' : status === 'config_required' ? 'warn' : 'muted'
    return { category, tone, status, notes: (row?.notes || []).join(' ') }
  })
}
