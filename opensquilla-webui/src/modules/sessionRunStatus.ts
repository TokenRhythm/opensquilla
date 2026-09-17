import i18n from '@/i18n'
import { normalizeTurnOutcome, turnOutcomePresentation } from '@/utils/chat/turnOutcome'

export interface SessionTaskSummary {
  startedAt: number | null
  finishedAt: number | null
  outcomePresentation?: string
}

const STATUS_ALIASES: Record<string, string> = {
  abandoned: 'interrupted',
  killed: 'cancelled',
  succeeded: 'idle',
  success: 'idle',
  complete: 'idle',
}
const KNOWN_STATUSES = new Set([
  'queued', 'running', 'interrupted', 'failed', 'timeout', 'cancelled', 'idle',
])
const TERMINAL_STATUSES = new Set(['failed', 'timeout', 'cancelled', 'interrupted'])
const STATUS_LABEL_KEYS: Record<string, string> = {
  queued: 'sessions.status.queued',
  running: 'sessions.status.running',
  failed: 'sessions.status.failed',
  timeout: 'sessions.status.timeout',
}

export function normalizeSessionRunStatus(status: string | undefined): string {
  const value = String(status || '').toLowerCase()
  return STATUS_ALIASES[value] || (KNOWN_STATUSES.has(value) ? value : 'idle')
}

export function summarizeSessionTask(value: unknown): SessionTaskSummary | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const task = value as Record<string, unknown>
  const startedAt = Number(task.started_at)
  const finishedAt = Number(task.finished_at)
  const outcome = normalizeTurnOutcome(task)
  return {
    startedAt: Number.isFinite(startedAt) ? startedAt : null,
    finishedAt: Number.isFinite(finishedAt) ? finishedAt : null,
    outcomePresentation: outcome ? turnOutcomePresentation(outcome) : undefined,
  }
}

export function resolveSessionRunStatus(
  declaredStatus?: string,
  activeStatusValue?: string,
  terminalStatusValue?: string,
): string {
  const activeStatus = activeStatusValue ? normalizeSessionRunStatus(activeStatusValue) : ''
  const lastStatus = normalizeSessionRunStatus(terminalStatusValue)
  const terminal = TERMINAL_STATUSES.has(lastStatus) ? lastStatus : ''
  if (activeStatus === 'queued' || activeStatus === 'running') return activeStatus
  if (terminal) return terminal
  return normalizeSessionRunStatus(declaredStatus || activeStatusValue)
}

export function sessionRunStatusLabel(
  status: string,
  task?: SessionTaskSummary | null,
): string {
  const t = i18n.global.t
  if (status === 'cancelled' || status === 'interrupted') {
    const presentation = task?.outcomePresentation || (status === 'cancelled' ? 'stopped' : 'interrupted')
    if (presentation === 'interrupted') return t('sessions.status.interrupted')
    if (task?.startedAt != null && task.finishedAt != null && task.finishedAt >= task.startedAt) {
      const seconds = Math.max(1, Math.round((task.finishedAt - task.startedAt) / 1000))
      return t('sessions.status.stoppedAfterSeconds', { seconds })
    }
    return t('sessions.status.cancelled')
  }
  return t(STATUS_LABEL_KEYS[status] || 'sessions.status.idle')
}
