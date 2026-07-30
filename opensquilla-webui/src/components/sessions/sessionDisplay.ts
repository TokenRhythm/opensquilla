import i18n from '@/i18n'
import type { IconName } from '@/utils/icons'
import type { SessionItem } from '@/composables/useSessions'

export interface SessionStatusBadge {
  label: string
  cls: string
}

export interface SessionAgentIdentity {
  kind: 'unknown' | 'known' | 'raw' | 'deleted'
  value: string
}

/** Only call an agent deleted after one authoritative agents.list succeeded. */
export function sessionAgentIdentity(
  id: string | null | undefined,
  agentNames: ReadonlyMap<string, string>,
  agentsLoaded: boolean,
): SessionAgentIdentity {
  const normalized = String(id || '').trim()
  if (!normalized || normalized === 'unknown') return { kind: 'unknown', value: '' }
  const known = agentNames.get(normalized)
  if (known) return { kind: 'known', value: known }
  return agentsLoaded
    ? { kind: 'deleted', value: normalized }
    : { kind: 'raw', value: normalized }
}

export type SessionStatusBadgeClasses = Partial<Record<
  'needsInput' | 'running' | 'queued' | 'failed' | 'timeout' | 'interrupted' | 'cancelled',
  string
>>

const DEFAULT_STATUS_CLASSES: Required<SessionStatusBadgeClasses> = {
  needsInput: 'is-needs-input',
  running: 'is-running',
  queued: 'is-queued',
  failed: 'is-failed',
  timeout: 'is-failed',
  interrupted: 'is-queued',
  cancelled: 'is-off',
}

export function sessionSurfaceIcon(item: SessionItem): IconName {
  if (item.sessionKind === 'cron') return 'cron'
  if (item.sessionKind === 'channel') return 'channels'
  if (item.sessionKind === 'task' || item.surface === 'subagent') return 'agents'
  if (item.sessionKind === 'chat') return 'chat'
  return 'sessions'
}

export function sessionStatusBadge(
  item: SessionItem,
  needsInput = false,
  classes: SessionStatusBadgeClasses = {},
): SessionStatusBadge | null {
  const t = i18n.global.t
  if (needsInput) {
    return {
      label: t('sessions.status.needsInput'),
      cls: classes.needsInput || DEFAULT_STATUS_CLASSES.needsInput,
    }
  }
  const map: Record<string, { labelKey: string; classKey: keyof SessionStatusBadgeClasses }> = {
    running: { labelKey: 'sessions.status.running', classKey: 'running' },
    queued: { labelKey: 'sessions.status.queued', classKey: 'queued' },
    failed: { labelKey: 'sessions.status.failed', classKey: 'failed' },
    timeout: { labelKey: 'sessions.status.timeout', classKey: 'timeout' },
    interrupted: { labelKey: 'sessions.status.interrupted', classKey: 'interrupted' },
    cancelled: { labelKey: 'sessions.status.cancelled', classKey: 'cancelled' },
  }
  const entry = map[item.runStatus]
  if (!entry) return null
  const stoppedLabel =
    (item.runStatus === 'cancelled' || item.runStatus === 'interrupted') && item.runLabel
      ? item.runLabel
      : ''
  return {
    label: stoppedLabel || t(entry.labelKey),
    cls: classes[entry.classKey] || DEFAULT_STATUS_CLASSES[entry.classKey],
  }
}

/**
 * Shared relative-time formatter for every session ledger row (and any other
 * surface that lists sessions). Renders "just now" / "Ns ago" / "Nm ago" /
 * "Nh ago" / "Nd ago" up to ~7 days, then falls back to an absolute date.
 */
export function formatRelativeTime(timestamp: number | null | undefined): string {
  if (!timestamp) return '—'
  const d = new Date(timestamp)
  if (isNaN(d.getTime())) return '—'

  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHour = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHour / 24)

  const t = i18n.global.t
  if (diffSec < 10) return t('sessions.relTime.justNow')
  if (diffSec < 60) return t('sessions.relTime.seconds', { n: diffSec })
  if (diffMin < 60) return t('sessions.relTime.minutes', { n: diffMin })
  if (diffHour < 24) return t('sessions.relTime.hours', { n: diffHour })
  if (diffDay < 7) return t('sessions.relTime.days', { n: diffDay })
  return d.toLocaleDateString()
}

/** Back-compat alias; prefer {@link formatRelativeTime} for new call sites. */
export const sessionRelTime = formatRelativeTime

/**
 * Ledger title for a subagent row: "↳ Subagent · {parent title}" when the
 * parent title is known, otherwise a plain "↳ Subagent" so we never surface a
 * raw key. The arrow + label conveys lineage without rendering UUIDs.
 */
export function subagentRowTitle(parentTitle: string | null | undefined): string {
  const t = i18n.global.t
  const parent = (parentTitle || '').trim()
  return parent
    ? `↳ ${t('sessions.ledger.subagentWithParent', { parent })}`
    : `↳ ${t('sessions.ledger.subagent')}`
}
