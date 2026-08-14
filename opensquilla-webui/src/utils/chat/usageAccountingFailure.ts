import type { StatusPart } from '@/types/parts'

export const USAGE_ACCOUNTING_BUSY = 'usage_accounting_busy'
export const USAGE_ACCOUNTING_UNAVAILABLE = 'usage_accounting_unavailable'

const USAGE_ACCOUNTING_CODES = new Set([
  USAGE_ACCOUNTING_BUSY,
  USAGE_ACCOUNTING_UNAVAILABLE,
])

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim().toLowerCase() : ''
}

export function isUsageAccountingBarrier(code: unknown): boolean {
  return USAGE_ACCOUNTING_CODES.has(text(code))
}

export function usageAccountingErrorCode(value: unknown): string | undefined {
  const outer = record(value)
  const outcome = record(outer.turn_outcome ?? outer.turnOutcome ?? outer.outcome)
  for (const candidate of [
    outer.code,
    outer.error_class,
    outer.errorClass,
    outcome.error_class,
    outcome.errorClass,
    outcome.reason,
  ]) {
    const code = text(candidate)
    if (USAGE_ACCOUNTING_CODES.has(code)) return code
  }
  return undefined
}

function phaseStatus(kind: string, phase: string, at: number): StatusPart | undefined {
  if (kind === 'router' && phase === 'decided') {
    return { action: 'router:decided', label: 'Selecting model', at, durability: 'durable' }
  }
  if (kind === 'state') {
    if (phase === 'thinking') return { action: 'Planning next step', label: 'Planning next step', at, durability: 'durable' }
    if (phase === 'streaming') return { action: 'Model is generating', label: 'Model is generating', at, durability: 'durable' }
    if (phase === 'tool_calling') return { action: 'Preparing tool call', label: 'Preparing tool call', at, durability: 'durable' }
    return undefined
  }
  if (kind !== 'provider') return undefined
  if (phase === 'requesting') return { action: 'provider:requesting', label: 'Waiting for model', at, durability: 'durable' }
  if (phase === 'reasoning') return { action: 'provider:reasoning', label: 'Thinking deeply', at, durability: 'durable' }
  if (phase === 'retry_wait') return { action: 'provider:retry_wait:0', label: 'Waiting to retry', at, durability: 'durable' }
  if (phase === 'retrying') return { action: 'provider:retrying:0:0', label: 'Retrying', at, durability: 'durable' }
  if (phase === 'fallback') return { action: 'provider:fallback', label: 'Switching to backup model', at, durability: 'durable' }
  return undefined
}

export function terminalActivityStatusHistory(
  value: unknown,
  expectedTurnId?: string,
): StatusPart[] {
  const snapshot = record(value)
  if (snapshot.version !== 1 || !Array.isArray(snapshot.phases)) return []
  const turnId = typeof snapshot.turn_id === 'string' ? snapshot.turn_id.trim() : ''
  const taskId = typeof snapshot.task_id === 'string' ? snapshot.task_id.trim() : ''
  if (expectedTurnId && ((turnId && turnId !== expectedTurnId) || (taskId && taskId !== expectedTurnId))) return []

  const statuses: StatusPart[] = []
  const seen = new Set<string>()
  for (const raw of snapshot.phases.slice(0, 32)) {
    const phase = record(raw)
    const kind = text(phase.kind)
    const name = text(phase.phase)
    const at = Number(phase.at)
    if (!Number.isFinite(at) || at <= 0) continue
    const status = phaseStatus(kind, name, at)
    if (!status) continue
    const identity = `${status.action}\u0000${status.at}`
    if (seen.has(identity)) continue
    seen.add(identity)
    statuses.push(status)
  }
  return statuses
}
