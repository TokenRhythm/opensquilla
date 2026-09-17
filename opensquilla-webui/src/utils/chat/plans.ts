import type {
  CollaborationMode,
  CollaborationSnapshot,
  PlanRevisionSnapshot,
  PlanRevisionStep,
  PlanRunSnapshot,
  PlanRunStatus,
  PlanRunStepSnapshot,
  PlanRunStepStatus,
} from '@/types/plans'

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function stringField(source: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

function numberField(source: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const raw = source[key]
    if (raw === null || raw === undefined || raw === '') continue
    const value = Number(raw)
    if (Number.isFinite(value) && value >= 0) return value
  }
  return undefined
}

function booleanField(source: Record<string, unknown>, ...keys: string[]): boolean | undefined {
  for (const key of keys) {
    const value = source[key]
    if (typeof value === 'boolean') return value
  }
  return undefined
}

function nestedSnapshot(value: unknown, keys: string[]): unknown {
  const source = record(value)
  if (!source) return value
  for (const key of keys) {
    if (source[key] !== undefined) return source[key]
  }
  return value
}

function normalizePlanStep(value: unknown): PlanRevisionStep | null {
  const source = record(value)
  if (!source) return null
  const stepId = stringField(source, 'stepId', 'step_id', 'id')
  const title = stringField(source, 'title', 'label')
  if (!stepId || !title) return null
  const details = stringField(source, 'details', 'description')
  return {
    stepId,
    title,
    ...(details ? { details } : {}),
  }
}

const RUN_STATUSES = new Set<PlanRunStatus>([
  'queued',
  'running',
  'paused',
  'blocked',
  'completed',
  'cancelled',
  'superseded',
])

const STEP_STATUSES = new Set<PlanRunStepStatus>([
  'pending',
  'in_progress',
  'completed',
  'blocked',
  'skipped',
])

function normalizeRunStep(value: unknown): PlanRunStepSnapshot | null {
  const source = record(value)
  if (!source) return null
  const stepId = stringField(source, 'stepId', 'step_id', 'id')
  const title = stringField(source, 'title', 'label')
  const rawStatus = stringField(source, 'status', 'state')
  if (!stepId || !title || !STEP_STATUSES.has(rawStatus as PlanRunStepStatus)) return null
  const reason = stringField(source, 'reason')
  return {
    stepId,
    title,
    status: rawStatus as PlanRunStepStatus,
    ...(reason ? { reason } : {}),
  }
}

export function normalizeCollaborationSnapshot(
  value: unknown,
  fallback: CollaborationSnapshot = { mode: 'default', revision: 0 },
): CollaborationSnapshot {
  const source = record(nestedSnapshot(value, ['collaboration'])) ?? {}
  const rawMode = stringField(source, 'mode', 'collaborationMode', 'collaboration_mode')
  const mode: CollaborationMode = rawMode === 'plan'
    ? 'plan'
    : rawMode === 'default'
      ? 'default'
      : fallback.mode
  return {
    mode,
    revision: numberField(
      source,
      'revision',
      'collaborationRevision',
      'collaboration_revision',
    ) ?? fallback.revision,
  }
}

export function normalizePlanRevisionSnapshot(
  value: unknown,
  currentRevisionId = '',
): PlanRevisionSnapshot | null {
  const source = record(nestedSnapshot(value, [
    'snapshot',
    'plan',
    'planRevision',
    'plan_revision',
    'currentPlan',
    'current_plan',
  ]))
  if (!source) return null
  const revisionId = stringField(source, 'revisionId', 'revision_id')
  const planId = stringField(source, 'planId', 'plan_id')
  const title = stringField(source, 'title')
  if (!revisionId || !planId || !title) return null
  const stepsRaw = Array.isArray(source.steps) ? source.steps : []
  const parentRevisionId = stringField(source, 'parentRevisionId', 'parent_revision_id')
  const generation = numberField(source, 'generation')
  const createdAt = numberField(source, 'createdAt', 'created_at')
  const explicitCurrent = booleanField(source, 'current')
  return {
    revisionId,
    planId,
    ...(parentRevisionId ? { parentRevisionId } : {}),
    ...(generation !== undefined ? { generation } : {}),
    title,
    markdown: typeof source.markdown === 'string' ? source.markdown : '',
    steps: stepsRaw.flatMap(step => normalizePlanStep(step) ?? []),
    current: explicitCurrent ?? (Boolean(currentRevisionId) && revisionId === currentRevisionId),
    ...(createdAt !== undefined ? { createdAt } : {}),
  }
}

export function normalizePlanRunSnapshot(value: unknown): PlanRunSnapshot | null {
  const source = record(nestedSnapshot(value, [
    'snapshot',
    'run',
    'planRun',
    'plan_run',
    'activePlanRun',
    'active_plan_run',
  ]))
  if (!source) return null
  const runId = stringField(source, 'runId', 'run_id')
  const planRevisionId = stringField(source, 'planRevisionId', 'plan_revision_id')
  const rawStatus = stringField(source, 'status')
  if (!runId || !planRevisionId || !RUN_STATUSES.has(rawStatus as PlanRunStatus)) return null
  const stepsRaw = Array.isArray(source.steps)
    ? source.steps
    : Array.isArray(source.stepStates)
      ? source.stepStates
      : Array.isArray(source.step_states)
        ? source.step_states
        : []
  const currentStepId = stringField(source, 'currentStepId', 'current_step_id')
  const stateRevision = numberField(source, 'stateRevision', 'state_revision')
  const driverKind = stringField(source, 'driverKind', 'driver_kind')
  const driverId = stringField(source, 'driverId', 'driver_id')
  const activeTaskId = stringField(source, 'activeTaskId', 'active_task_id')
  const pauseReason = stringField(source, 'pauseReason', 'pause_reason')
  const terminalReason = stringField(source, 'terminalReason', 'terminal_reason')
  const createdAt = numberField(source, 'createdAt', 'created_at')
  const updatedAt = numberField(source, 'updatedAt', 'updated_at')
  const startedAt = numberField(source, 'startedAt', 'started_at')
  const finishedAt = numberField(source, 'finishedAt', 'finished_at')
  return {
    runId,
    planRevisionId,
    status: rawStatus as PlanRunStatus,
    ...(currentStepId ? { currentStepId } : {}),
    ...(stateRevision !== undefined ? { stateRevision } : {}),
    ...(driverKind ? { driverKind } : {}),
    ...(driverId ? { driverId } : {}),
    ...(activeTaskId ? { activeTaskId } : {}),
    ...(pauseReason ? { pauseReason } : {}),
    ...(terminalReason ? { terminalReason } : {}),
    ...(createdAt !== undefined ? { createdAt } : {}),
    ...(updatedAt !== undefined ? { updatedAt } : {}),
    ...(startedAt !== undefined ? { startedAt } : {}),
    ...(finishedAt !== undefined ? { finishedAt } : {}),
    steps: stepsRaw.flatMap(step => normalizeRunStep(step) ?? []),
  }
}

export function planRevisionsFromToolSegments(
  segments: unknown,
  currentRevisionId = '',
): PlanRevisionSnapshot[] {
  if (!Array.isArray(segments)) return []
  return segments.flatMap(segment => {
    const source = record(segment)
    if (!source || source.type !== 'plan') return []
    const snapshot = normalizePlanRevisionSnapshot(source, currentRevisionId)
    return snapshot ? [snapshot] : []
  })
}

export function payloadBelongsToSession(value: unknown, sessionKey: string): boolean {
  const source = record(value)
  if (!source || !sessionKey) return false
  const key = stringField(source, 'sessionKey', 'session_key', 'key')
  return !key || key === sessionKey
}
