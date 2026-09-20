import type {
  TracePhase,
  TracePhaseName,
  TraceProjection,
  TraceSpan,
  TraceSpanStatus,
  TraceDetails,
} from '@/types/traceView'

export type TraceTimelineLane = 'intake' | 'model' | 'tool' | 'control' | 'result'
export type TraceDisplayCategory = 'input' | 'context' | 'model' | 'tool' | 'output' | 'result'
  | 'routing' | 'retry' | 'fallback' | 'approval' | 'maintenance' | 'subagent' | 'unknown'

export interface TraceDisplayItem {
  row: TraceSpan
  lane: TraceTimelineLane
  category: TraceDisplayCategory
  boundary: boolean
  index: number
}

const BOUNDARY_CATEGORIES = new Set<TraceDisplayCategory>([
  'routing', 'retry', 'fallback', 'approval', 'maintenance', 'subagent',
])

const TERMINAL_KINDS = new Set(['turn_end', 'turn_error', 'turn_cancelled', 'turn_canceled'])

/** Finalization work is not itself evidence that the run has ended. */
export function isTraceTerminal(row: TraceSpan): boolean {
  return TERMINAL_KINDS.has(row.kind.toLowerCase())
}

/** A recorded result can carry an error even in older rows marked successful. */
export function traceTerminalStatus(row: TraceSpan): TraceSpanStatus {
  if (!isTraceTerminal(row)) return row.status
  const kind = row.kind.toLowerCase()
  if (kind === 'turn_cancelled' || kind === 'turn_canceled' || row.status === 'cancelled') return 'cancelled'
  const error = tracePayload(row).error
  const hasError = typeof error === 'string' ? error.trim().length > 0
    : error != null && error !== false && (typeof error !== 'object' || Object.keys(error).length > 0)
  if (kind === 'turn_error' || row.status === 'error' || hasError) return 'error'
  return 'success'
}

/** Classify recorded operations, independently from their display lane. */
export function traceEventCategory(row: TraceSpan): TraceDisplayCategory {
  const kind = row.kind.toLowerCase()
  if (isTraceTerminal(row)) return 'result'
  // Response payloads may mention retries or approvals; only the operation's
  // recorded name and phase determine its category.
  if (['llm_request', 'llm_progress', 'llm_response', 'llm_error'].includes(kind)) return 'model'
  if (['tool_request', 'tool_response'].includes(kind)) return 'tool'
  if (['agent_runtime_budget', 'image_input_preflight', 'tool_projection_noop', 'tool_provider_projection_noop', 'tool_projection_applied'].includes(kind)) return 'context'
  if (kind.includes('fallback')) return 'fallback'
  if (kind.includes('retry')) return 'retry'
  if (row.phase === 'approval_sandbox' || /approval|sandbox|permission/.test(kind)) return 'approval'
  if (row.phase === 'compaction_maintenance' || /compact|maintenance/.test(kind)) return 'maintenance'
  if (row.phase === 'subagent' || /subagent|child_agent|child_run|delegate/.test(kind)) return 'subagent'
  if (row.phase === 'routing' || /route|routing|router|ensemble/.test(kind)) return 'routing'
  if (row.phase === 'finalize' || ['finalize', 'response_ready'].includes(kind)) return 'output'
  if (row.phase === 'tool_execution' || kind.includes('tool')) return 'tool'
  if (row.phase === 'model_execution' || /model|provider|llm|completion/.test(kind)) return 'model'
  if (row.phase === 'context' || /context|prompt|history/.test(kind)) return 'context'
  if (row.phase === 'intake' || /turn_start|input_received/.test(kind)) return 'input'
  return 'unknown'
}

/** Keep event order and timestamps intact while locating context in its run. */
export function traceDisplayItems(rows: TraceSpan[]): TraceDisplayItem[] {
  return rows.map((row, index) => {
    const category = traceEventCategory(row)
    const lane: TraceTimelineLane = category === 'input' || category === 'context' ? 'intake'
      : category === 'model' ? 'model' : category === 'tool' ? 'tool'
        : category === 'result' ? 'result' : 'control'
    return { row, lane, category, boundary: BOUNDARY_CATEGORIES.has(category) || category === 'result', index }
  })
}

export interface TraceSemanticStep extends TraceDisplayItem {
  id: string
  rawIds: string[]
  label?: 'preparation' | 'contextAdjusted' | 'contextUncertain' | 'toolResultTransformed' | 'imageProcessed' | 'imageRejected'
  status?: TraceSpanStatus
  change?: {
    beforeId?: string
    afterId: string
    afterInputId?: string
    removedMessages?: number
    uncertain?: boolean
  }
}

const PREPARATION_STAGES = new Set(['session:loaded', 'prompt:before', 'prompt:images', 'stream:context'])
const ADJUSTMENT_STAGES = new Set(['session:sanitized', 'session:limited'])
const TOOL_PROJECTION_NOOPS = new Set(['tool_projection_noop', 'tool_provider_projection_noop'])

function tracePayload(row: TraceSpan): Record<string, unknown> {
  return { ...row.attrs, ...asRecord(row.output), ...asRecord(row.input) }
}

function contextStage(row: TraceSpan): string | undefined {
  return stringValue(tracePayload(row).stage, row.summary, row.kind === 'context_stage' ? undefined : row.kind)
}

function operationId(row: TraceSpan): string | undefined {
  const payload = tracePayload(row)
  return stringValue(row.logicalCallId, payload.call_id, payload.tool_use_id)
}

function completeMessages(row: TraceSpan | undefined): unknown[] | undefined {
  if (!row || row.inputTruncated || row.outputTruncated) return undefined
  const payload = tracePayload(row)
  const messages = payload.messages
  if (!Array.isArray(messages) || payload.messages_truncated === true) return undefined
  if (typeof payload.message_count === 'number' && payload.message_count !== messages.length) return undefined
  if (messages.some(message => {
    const record = asRecord(message)
    return !record || typeof record.role !== 'string' || !(typeof record.content === 'string' || Array.isArray(record.content))
  })) return undefined
  // A preview can be bounded at the message or field level as well as at the
  // outer payload. Masked values cannot prove two original contexts identical.
  const incomplete = (value: unknown): boolean => {
    if (typeof value === 'string') return /\[(?:redacted|truncated)\]|内容已截断/i.test(value)
    if (Array.isArray(value)) return value.some(incomplete)
    const record = asRecord(value)
    return !!record && Object.entries(record).some(([key, child]) =>
      (/(?:^|_)truncated$/.test(key) && child === true) || incomplete(child))
  }
  return messages.some(incomplete) ? undefined : messages
}

function canonicalContent(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalContent).join(',')}]`
  const record = asRecord(value)
  if (record) return `{${Object.keys(record).sort().map(key => `${JSON.stringify(key)}:${canonicalContent(record[key])}`).join(',')}}`
  return JSON.stringify(value) ?? 'null'
}

function contextChange(before: TraceSpan | undefined, after: TraceSpan): TraceSemanticStep['change'] & { changed: boolean } {
  const previous = completeMessages(before)
  const current = completeMessages(after)
  const payload = tracePayload(after)
  const removed = numberValue(payload.removed_messages)
  const sanitize = asRecord(payload.sanitize)
  const historical = asRecord(payload.historical_projection)
  const provenChange = (removed != null && removed > 0)
    || (numberValue(sanitize?.metadata_keys_removed) ?? 0) > 0
    || ['tool_uses_projected', 'tool_results_projected', 'reasoning_chars_removed']
      .some(key => (numberValue(historical?.[key]) ?? 0) > 0)
  if (previous && current) {
    const removedMessages = Math.max(0, previous.length - current.length)
    return {
      beforeId: before?.id, afterId: after.id,
      changed: provenChange || canonicalContent(previous) !== canonicalContent(current),
      removedMessages: removedMessages || undefined,
      uncertain: removed != null && removed > 0 && removed !== removedMessages ? true : undefined,
    }
  }
  return {
    beforeId: before?.id, afterId: after.id, changed: provenChange, uncertain: true,
    removedMessages: removed != null && removed > 0 ? removed : undefined,
  }
}

/** A lossless display projection: diagnostics stay attached to their operation. */
export function projectTraceSteps(rows: TraceSpan[]): TraceSemanticStep[] {
  const items = traceDisplayItems(rows)
  type Attachment = 'input' | 'model' | 'tool' | 'result'
  const attachedTo = new Map<number, Attachment>()
  const steps = new Map<number, TraceSemanticStep>()
  const barriers = new Set<number>()
  let previousSnapshot: TraceSpan | undefined

  for (const item of items) {
    const { row, index } = item
    const payload = tracePayload(row)
    const stage = contextStage(row)
    const terminal = row.status === 'error' || row.status === 'cancelled'
      || /(?:^|_)(?:error|cancelled|canceled|cancel|abort)(?:_|$)/.test(row.kind)
    const step: TraceSemanticStep = { ...item, id: row.id, rawIds: [row.id], boundary: item.boundary || terminal }
    if (isTraceTerminal(row)) step.status = traceTerminalStatus(row)
    if (step.boundary || item.category === 'unknown') {
      barriers.add(index)
      previousSnapshot = undefined
    }
    if (row.kind === 'image_input_preflight') {
      const action = stringValue(payload.action)?.toLowerCase()
      const count = numberValue(payload.image_count)
      if (action === 'reject' || action === 'error' || action === 'failed') {
        step.label = 'imageRejected'
        step.status = 'error'
        step.boundary = true
        barriers.add(index)
        previousSnapshot = undefined
      } else if (!step.boundary && action && !['noop', 'none', 'skip', 'allow', 'accept'].includes(action) && count !== 0) {
        step.label = 'imageProcessed'
      } else if (!step.boundary && (count === 0 || ['noop', 'none', 'skip', 'allow', 'accept'].includes(action || ''))) {
        attachedTo.set(index, 'model')
      }
    } else if (!step.boundary && ['prompt_report', 'agent_runtime_budget'].includes(row.kind)) {
      attachedTo.set(index, 'input')
    } else if (!step.boundary && row.kind === 'context_stage') {
      if (stage && ADJUSTMENT_STAGES.has(stage)) {
        const expectedBefore = stage === 'session:sanitized' ? 'session:loaded' : 'session:sanitized'
        const before = previousSnapshot && contextStage(previousSnapshot) === expectedBefore ? previousSnapshot : undefined
        const { changed, ...change } = contextChange(before, row)
        if (changed || change.uncertain) {
          step.label = changed ? 'contextAdjusted' : 'contextUncertain'
          step.change = change
        } else attachedTo.set(index, 'model')
      } else if (stage && PREPARATION_STAGES.has(stage)) attachedTo.set(index, 'model')
      else if (stage === 'session:after') attachedTo.set(index, 'result')
      else barriers.add(index)
      // Only adjacent, known history snapshots establish a before/after pair.
      previousSnapshot = stage && (PREPARATION_STAGES.has(stage) || ADJUSTMENT_STAGES.has(stage)) ? row : undefined
    } else if (!step.boundary && TOOL_PROJECTION_NOOPS.has(row.kind)) {
      attachedTo.set(index, 'tool')
    } else if (!step.boundary && row.kind === 'tool_projection_applied') {
      step.label = 'toolResultTransformed'
      step.change = { afterId: row.id }
    } else if (item.category !== 'context') previousSnapshot = undefined
    steps.set(index, step)
  }

  const isOperation = (index: number, target: Attachment): boolean => {
    const item = items[index]
    if (target === 'model') return ['llm_request', 'llm_progress', 'llm_response', 'llm_error'].includes(item.row.kind)
    if (target === 'tool') return ['tool_request', 'tool_response'].includes(item.row.kind)
    if (target === 'input') return ['turn_start', 'input_received'].includes(item.row.kind)
    return isTraceTerminal(item.row)
  }
  const reachable = (from: number, to: number): boolean => {
    const left = Math.min(from, to)
    const right = Math.max(from, to)
    for (let index = left + 1; index < right; index++) if (barriers.has(index)) return false
    // The failed call may own its own preparation; its boundary only blocks
    // attributing records to operations beyond that call.
    return !barriers.has(from)
  }
  const matchingOperation = (from: number, target: Attachment): number | undefined => {
    const id = operationId(items[from].row)
    if (id && (target === 'model' || target === 'tool')) {
      const matches = items.filter(item => isOperation(item.index, target)
        && operationId(item.row) === id && reachable(from, item.index))
      // A logical identifier shared by several attempts is not enough evidence
      // to attribute a diagnostic to one physical attempt.
      return matches.length === 1 ? matches[0].index : undefined
    }
    const directions = target === 'result' ? [1, -1] : target === 'input' || target === 'tool' ? [-1, 1] : [1]
    for (const direction of directions) {
      for (let index = from + direction; index >= 0 && index < items.length; index += direction) {
        if (isOperation(index, target) && (target !== 'input' || !barriers.has(index))) return index
        if (barriers.has(index)) break
        if (target === 'result' && items[index].category === 'output') continue
        if (!attachedTo.has(index) && !['contextAdjusted', 'contextUncertain', 'imageProcessed', 'toolResultTransformed'].includes(steps.get(index)?.label || '')) break
      }
    }
    return undefined
  }

  const completedTrace = items.some(item => isTraceTerminal(item.row))
  for (const [index, target] of attachedTo) {
    let owner = matchingOperation(index, target)
    // Input metadata after a routing boundary prepares the upcoming call; it
    // cannot be moved backwards over the routing decision.
    if (owner == null && target === 'input') owner = matchingOperation(index, 'model')
    if (owner != null) {
      steps.get(owner)!.rawIds.push(items[index].row.id)
      steps.delete(index)
    } else if (target === 'tool' || target === 'result' || completedTrace
      || items.slice(index + 1).some(item => barriers.has(item.index)
        || ['input', 'model', 'tool', 'output', 'result', 'unknown'].includes(item.category))) {
      // An orphan diagnostic is still a recorded checkpoint, not evidence of
      // a call waiting to start. Only the live trailing preparation can queue.
      attachedTo.delete(index)
    }
  }

  for (const [index, step] of steps) {
    if (step.label !== 'toolResultTransformed') continue
    const owner = matchingOperation(index, 'tool')
    if (owner != null) step.change!.beforeId = items[owner].row.id
    const toolId = operationId(step.row)
    if (!toolId) continue
    for (let next = index + 1; next < items.length; next++) {
      if (barriers.has(next)) break
      if (!isOperation(next, 'model')) continue
      const messages = completeMessages(items[next].row)
      if (messages?.some(message => {
        const content = asRecord(message)?.content
        return Array.isArray(content) && content.some(block => {
          const record = asRecord(block)
          return record?.type === 'tool_result' && record.tool_use_id === toolId && 'content' in record
        })
      })) step.change!.afterInputId = items[next].row.id
      break
    }
  }

  // Until a call arrives, its diagnostics form one honest preparation item.
  // Recomputing with streamed rows moves the same raw IDs onto that call.
  let preparation: TraceSemanticStep | undefined
  for (const item of items) {
    const step = steps.get(item.index)
    if (barriers.has(item.index) || (step && !attachedTo.has(item.index)
      && !['contextAdjusted', 'contextUncertain', 'imageProcessed', 'toolResultTransformed'].includes(step.label || ''))) preparation = undefined
    if (!step || !attachedTo.has(item.index)) continue
    if (preparation && preparation.lane === item.lane) {
      preparation.rawIds.push(item.row.id)
      steps.delete(item.index)
    } else {
      const id = `preparation:${item.row.id}`
      const row: TraceSpan = {
        id, kind: 'trace_preparation', phase: 'context', title: 'Preparation', status: 'queued',
        recordedAt: item.row.recordedAt, elapsedMs: item.row.elapsedMs, seq: item.row.seq,
      }
      preparation = { ...item, row, id, rawIds: [item.row.id], label: 'preparation', boundary: false }
      steps.set(item.index, preparation)
    }
  }
  const rawOrder = new Map(rows.map((row, index) => [row.id, index]))
  return [...steps.values()].map((step, index) => ({
    ...step, index, rawIds: [...new Set(step.rawIds)].sort((a, b) => (rawOrder.get(a) ?? 0) - (rawOrder.get(b) ?? 0)),
  }))
}

/**
 * Collapse adjacent context checkpoints for the timeline view.
 *
 * Context checkpoints are durable diagnostics (session loaded, sanitised,
 * prompt assembled, and so on), rather than separate work intervals. Keep
 * the individual rows in the ledger/inspector, but represent one contiguous
 * run as a sample range. The range is not a measured operation duration.
 */
export function groupTraceRowsForTimeline(rows: TraceSpan[]): TraceSpan[] {
  const grouped: TraceSpan[] = []
  let contextRun: TraceSpan[] = []
  let contextLane: TraceTimelineLane | undefined

  const flushContext = () => {
    if (!contextRun.length) return
    if (contextRun.length === 1) {
      grouped.push(contextRun[0])
    } else {
      const first = contextRun[0]
      const last = contextRun[contextRun.length - 1]
      const samples = contextRun.map(row => row.recordedAt ?? row.elapsedMs ?? row.startedAt).filter((value): value is number => value != null)
      const stages = contextRun.map(row => row.summary || row.kind)
      const startedAt = samples.length ? Math.min(...samples) : undefined
      const endedAt = samples.length ? Math.max(...samples) : undefined
      grouped.push({
        id: `context-group:${first.id}:${last.id}`,
        kind: 'context_stage_group',
        phase: 'context',
        title: 'Context preparation',
        status: contextRun.some(row => row.status === 'error')
          ? 'error'
          : contextRun.some(row => row.status === 'running')
            ? 'running'
            : contextRun.every(row => row.status === 'success') ? 'success' : 'unknown',
        startedAt,
        endedAt,
        seq: first.seq,
        summary: stages.join(' → '),
        attrs: {
          grouped: true,
          display_lane: contextLane,
          context_count: contextRun.length,
          context_stages: stages,
          context_ids: contextRun.map(row => row.id),
        },
      })
    }
    contextRun = []
    contextLane = undefined
  }

  for (const { row, lane, category, boundary } of traceDisplayItems(rows)) {
    if (row.kind === 'context_stage' && category === 'context' && !boundary && row.durationMs == null && contextStage(row) !== 'session:after') {
      if (contextRun.length && contextLane !== lane) flushContext()
      contextLane = lane
      contextRun.push(row)
    } else {
      flushContext()
      grouped.push(row)
    }
  }
  flushContext()
  return grouped
}

export interface TraceTimelineItem {
  row: TraceSpan
  lane: TraceTimelineLane
  kind: 'point' | 'span' | 'checkpoints'
  start: number
  end: number
  stack: number
}

export function traceTimelineLane(row: TraceSpan, priorRows: TraceSpan[] = []): TraceTimelineLane {
  const items = traceDisplayItems([...priorRows, row])
  return items[items.length - 1].lane
}

/** Select within the clicked group; the original event remains the inspector source. */
export function traceTimelineSelection(row: TraceSpan, rows: TraceSpan[]): string | null {
  const ids = row.attrs?.context_ids
  return row.attrs?.grouped === true && Array.isArray(ids)
    ? rows.find(candidate => ids.includes(candidate.id))?.id ?? null
    : row.id
}

/** Preserve recorded time. Only vertical placement changes when symbols collide. */
export function layoutTraceTimeline(rows: TraceSpan[], trackWidth = 600): {
  items: TraceTimelineItem[]; min: number; max: number; span: number
} {
  const items = traceDisplayItems(rows).flatMap(({ row, lane }): TraceTimelineItem[] => {
    const point = row.recordedAt ?? row.elapsedMs ?? row.startedAt
    const checkpoints = row.attrs?.grouped === true
    const measured = !checkpoints && !isTraceTerminal(row) && row.durationMs != null && row.durationMs > 0 && row.startedAt != null
    const start = checkpoints || measured ? row.startedAt : point
    if (start == null || !Number.isFinite(start)) return []
    const end = checkpoints ? row.endedAt ?? start
      : measured ? row.endedAt ?? start + row.durationMs! : start
    if (!Number.isFinite(end) || end < start) return []
    return [{ row, lane, kind: checkpoints ? 'checkpoints' : measured ? 'span' : 'point', start, end, stack: 0 }]
  })
  const relative = rows.some(row => row.elapsedMs != null || row.startedElapsedMs != null)
  const min = items.length ? Math.min(...items.map(item => item.start), ...(relative ? [0] : [])) : 0
  const max = items.length ? Math.max(...items.map(item => item.end)) : min
  const span = Math.max(1, max - min)
  const occupied = new Map<string, Array<Array<[number, number]>>>()
  for (const item of [...items].sort((a, b) => a.start - b.start)) {
    const x = (item.start - min) / span * trackWidth
    const right = (item.end - min) / span * trackWidth
    // Symbols have a fixed hit target; their horizontal anchor never moves.
    const bounds: [number, number] = item.kind === 'span' ? [x, right] : [x - 8, Math.max(x + (item.kind === 'checkpoints' ? 28 : 8), right)]
    const levels = occupied.get(item.lane) || []
    let level = levels.findIndex(ranges => ranges.every(([left, end]) => bounds[1] <= left || bounds[0] >= end))
    if (level < 0) { level = levels.length; levels.push([]) }
    levels[level].push(bounds)
    item.stack = level
    occupied.set(item.lane, levels)
  }
  return { items, min, max, span }
}

const PHASE_ORDER: TracePhaseName[] = [
  'intake', 'context', 'routing', 'model_execution', 'tool_execution',
  'approval_sandbox', 'compaction_maintenance', 'subagent', 'finalize', 'unknown',
]
const PHASE_LABELS: Record<TracePhaseName, string> = {
  intake: 'Intake',
  context: 'Context',
  routing: 'Routing',
  model_execution: 'Model execution',
  tool_execution: 'Tool execution',
  approval_sandbox: 'Approval & sandbox',
  compaction_maintenance: 'Maintenance',
  subagent: 'Sub-agent',
  finalize: 'Finalize',
  unknown: 'Other',
}

interface NormalizedEvent {
  traceId: string
  runId?: string
  turnId?: string
  seq?: number
  ts?: number
  kind: string
  attrs: Record<string, unknown>
  payload: Record<string, unknown>
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function stringValue(...values: unknown[]): string | undefined {
  return values.find(value => typeof value === 'string' && value.length > 0) as string | undefined
}

function numberValue(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string' && value.trim() !== '') {
      const n = Number(value)
      if (Number.isFinite(n)) return n
    }
  }
  return undefined
}

function eventFrom(value: unknown): NormalizedEvent | null {
  let row: Record<string, unknown> | null = asRecord(value)
  if (!row && typeof value === 'string') {
    try { row = asRecord(JSON.parse(value)) } catch { return null }
  }
  if (!row) return null
  const context = asRecord(row.context) || {}
  const attrs = asRecord(row.attrs) || {}
  const payload = asRecord(row.payload) || {}
  const kind = stringValue(row.kind, row.event, row.type)
  const traceId = stringValue(row.trace_id, row.traceId, context.trace_id, context.traceId)
  if (!kind || !traceId) return null
  const timestamp = row.ts ?? row.timestamp ?? row.time
  const parsedTs = numberValue(timestamp) ?? (typeof timestamp === 'string' ? Date.parse(timestamp) : undefined)
  return {
    traceId,
    runId: stringValue(row.run_id, row.runId, context.run_id, context.runId),
    turnId: stringValue(row.turn_id, row.turnId, context.turn_id, context.turnId),
    seq: numberValue(row.seq, attrs.seq),
    ts: parsedTs,
    kind,
    attrs,
    payload,
  }
}

function phaseFor(kind: string, attrs: Record<string, unknown>): TracePhaseName {
  const value = `${kind} ${stringValue(attrs.phase, attrs.stage) || ''}`.toLowerCase()
  if (value.includes('turn_start') || value.includes('intake')) return 'intake'
  if (['agent_runtime_budget', 'image_input_preflight', 'tool_projection_noop', 'tool_provider_projection_noop', 'tool_projection_applied'].includes(kind)) return 'context'
  if (value.includes('approval') || value.includes('sandbox') || value.includes('permission')) return 'approval_sandbox'
  if (value.includes('compact') || value.includes('maintenance') || value.includes('keepalive')) return 'compaction_maintenance'
  if (value.includes('subagent') || value.includes('child_agent')) return 'subagent'
  if (value.includes('route') || value.includes('routing') || value.includes('router') || value.includes('ensemble') || value.includes('fallback')) return 'routing'
  if (value.includes('context') || value.includes('prompt') || value.includes('history')) return 'context'
  if (value.includes('tool')) return 'tool_execution'
  if (value.includes('turn_end') || value.includes('final') || value.includes('cancel') || value.includes('error')) return 'finalize'
  if (value.includes('model') || value.includes('provider') || value.includes('llm') || value.includes('completion')) return 'model_execution'
  return 'unknown'
}

function statusFor(kind: string, attrs: Record<string, unknown>): TraceSpanStatus {
  const raw = stringValue(attrs.status, attrs.state, attrs.outcome)?.toLowerCase()
  if (raw === 'success' || raw === 'ok' || raw === 'completed' || raw === 'done') return 'success'
  if (raw === 'error' || raw === 'failed' || raw === 'failure') return 'error'
  if (raw === 'cancelled' || raw === 'canceled') return 'cancelled'
  if (raw === 'skipped') return 'skipped'
  if (raw === 'queued' || raw === 'pending') return 'queued'
  if (raw === 'running' || raw === 'started' || raw === 'active') return 'running'
  const lower = kind.toLowerCase()
  if (lower.includes('error') || lower.includes('failed')) return 'error'
  if (lower.includes('cancel')) return 'cancelled'
  if (lower.endsWith('_start') || lower.endsWith('.start') || lower.includes('started')) return 'running'
  if (lower.endsWith('_end') || lower.endsWith('.end') || lower.includes('completed')) return 'success'
  return 'unknown'
}

function titleFor(kind: string, phase: TracePhaseName, attrs: Record<string, unknown>): string {
  return stringValue(attrs.title, attrs.name, attrs.operation, attrs.tool_name, attrs.toolName)
    || kind.replace(/[._-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
    || PHASE_LABELS[phase]
}

/** Build a display-only projection. Unknown/malformed rows are ignored. */
export function projectTraceEvents(values: unknown[]): TraceProjection | null {
  const events = values.map(eventFrom).filter((event): event is NormalizedEvent => !!event)
  if (!events.length) return null
  // A log tail can contain several runs. The last observed trace is the one
  // users are most likely following in the live Logs view.
  const traceId = events[events.length - 1].traceId
  const traceEvents = events.filter(event => event.traceId === traceId)
  const spans: TraceSpan[] = traceEvents.map((event, index) => {
    const phase = phaseFor(event.kind, event.attrs)
    const status = traceTerminalStatus({
      id: '', kind: event.kind, phase, title: '', status: statusFor(event.kind, event.attrs),
      attrs: event.attrs, output: event.payload,
    })
    const duration = numberValue(event.attrs.duration_ms, event.attrs.durationMs, event.payload.duration_ms)
    const id = stringValue(event.attrs.span_id, event.attrs.spanId, event.payload.span_id, event.payload.spanId)
      || `${traceId}:${event.seq ?? index}`
    const parentId = stringValue(event.attrs.parent_span_id, event.attrs.parentSpanId, event.payload.parent_span_id)
    const links = []
    const retryOf = stringValue(event.attrs.retry_of, event.attrs.retryOf)
    const fallbackOf = stringValue(event.attrs.fallback_of, event.attrs.fallbackOf)
    if (retryOf) links.push({ type: 'retry' as const, spanId: retryOf })
    if (fallbackOf) links.push({ type: 'fallback' as const, spanId: fallbackOf })
    const completedKind = ['llm_response', 'llm_error', 'tool_response', 'approval_resolved'].includes(event.kind)
    const endedAt = completedKind && event.ts != null ? event.ts : undefined
    const startedAt = completedKind && event.ts != null && duration != null
      ? event.ts - duration
      : event.ts
    return {
      id,
      parentId,
      kind: event.kind,
      phase,
      title: titleFor(event.kind, phase, event.attrs),
      role: stringValue(event.attrs.role, event.attrs.call_kind, event.payload.role),
      provider: stringValue(event.attrs.provider, event.payload.provider),
      model: stringValue(event.attrs.model, event.payload.model),
      status,
      startedAt,
      recordedAt: event.ts,
      endedAt: endedAt ?? (status === 'success' || status === 'error' || status === 'cancelled'
        ? event.ts : undefined),
      durationMs: duration,
      seq: event.seq,
      attemptIndex: numberValue(event.attrs.attempt_index, event.attrs.attemptIndex),
      logicalCallId: stringValue(event.attrs.logical_call_id, event.attrs.logicalCallId),
      physicalAttemptId: stringValue(event.attrs.physical_attempt_id, event.attrs.physicalAttemptId),
      links,
      summary: stringValue(event.attrs.summary, event.attrs.message, event.payload.summary),
      attrs: event.attrs,
    }
  })
  const phases: TracePhase[] = PHASE_ORDER.map(name => {
    const phaseSpans = spans.filter(span => span.phase === name)
    const terminal = phaseSpans.some(span => span.status === 'error')
      ? 'error'
      : phaseSpans.some(span => span.status === 'running')
        ? 'running'
        : phaseSpans.some(span => span.status === 'success') ? 'success' : 'unknown'
    const starts = phaseSpans.map(span => span.startedAt).filter((n): n is number => n != null)
    const ends = phaseSpans.map(span => span.endedAt).filter((n): n is number => n != null)
    return {
      name,
      label: PHASE_LABELS[name],
      status: terminal as TraceSpanStatus,
      spans: phaseSpans,
      durationMs: name !== 'context' && starts.length && ends.length ? Math.max(0, Math.max(...ends) - Math.min(...starts)) : undefined,
    }
  }).filter(phase => phase.spans.length)
  const terminalSpan = [...spans].reverse().find(isTraceTerminal)
  const finalStatus: TraceSpanStatus = terminalSpan ? traceTerminalStatus(terminalSpan) : spans.some(span => span.status === 'error')
    ? 'error'
    : spans.some(span => span.status === 'cancelled') ? 'cancelled'
      : spans.some(span => span.status === 'running') ? 'running' : 'unknown'
  const last = traceEvents[traceEvents.length - 1]
  const route = traceEvents.find(event => phaseFor(event.kind, event.attrs) === 'routing')
  return {
    traceId,
    runId: traceEvents.find(event => event.runId)?.runId,
    turnId: traceEvents.find(event => event.turnId)?.turnId,
    status: finalStatus,
    requestedMode: route && stringValue(route.attrs.requested_mode, route.attrs.requestedMode, route.payload.requested_mode),
    effectiveMode: route && stringValue(route.attrs.effective_mode, route.attrs.effectiveMode, route.payload.effective_mode),
    phases,
    spans,
    currentSeq: last.seq,
    complete: terminalSpan != null,
  }
}

/** Normalize the privacy-preserving backend projection for the Vue view. */
export function projectionFromApi(value: unknown): TraceProjection | null {
  const row = asRecord(value)
  if (!row) return null
  const traceId = stringValue(row.trace_id, row.traceId)
  if (!traceId) return null
  const rawSpans = Array.isArray(row.spans) ? row.spans : []
  const spans: TraceSpan[] = rawSpans.flatMap((value, index) => {
    const span = asRecord(value)
    if (!span) return []
    const phase = (stringValue(span.phase) || 'unknown') as TracePhaseName
    const status = stringValue(span.status) || 'unknown'
    const timestamp = numberValue(span.ts) ?? (typeof span.ts === 'string' ? Date.parse(span.ts) : undefined)
    const elapsed = numberValue(span.elapsed_ms, span.elapsedMs)
    const startedElapsed = numberValue(span.started_elapsed_ms, span.startedElapsedMs)
    const endedElapsed = numberValue(span.ended_elapsed_ms, span.endedElapsedMs)
    const startedTimestamp = numberValue(span.started_ts, span.startedAt)
      ?? (typeof span.started_ts === 'string' ? Date.parse(span.started_ts) : undefined)
    const unfinishedCall = ['llm_request', 'llm_progress', 'tool_request', 'approval_wait'].includes(stringValue(span.kind) || '')
    const duration = unfinishedCall ? undefined : numberValue(span.duration_ms, span.durationMs)
    // Raw response records are emitted after the provider/tool completes. Their
    // timestamp is therefore the end of the interval, unlike checkpoint events.
    // Keep the ledger order unchanged but anchor the bar at the actual start.
    const completedKind = ['llm_response', 'llm_error', 'tool_response', 'approval_resolved'].includes(stringValue(span.kind) || '')
    // Prefer the turn-relative monotonic endpoint whenever it is present so
    // completed rows stay in the same coordinate system as checkpoint rows.
    // ``ts`` is an epoch timestamp and mixing it with elapsed milliseconds
    // would stretch the timeline and make adjacent stages appear to overlap.
    const completedEnd = completedKind ? (endedElapsed ?? elapsed ?? timestamp) : undefined
    const endedAt = completedKind ? completedEnd : unfinishedCall ? undefined : elapsed
    // A completed record's `ts` is its append/end time. Prefer the paired
    // request timestamp when it is available: this preserves the causal
    // observed boundary between calls. Keep the measured duration separately
    // from the observed endpoints. Older records without a paired request
    // fall back to end minus duration.
    const startedAt = unfinishedCall
      ? startedElapsed ?? (elapsed == null ? startedTimestamp : undefined) ?? elapsed ?? timestamp
      : completedKind && startedElapsed != null
      ? startedElapsed
      : completedKind && elapsed != null && duration != null
      ? elapsed - duration
      : completedKind && startedTimestamp != null && elapsed == null
      ? startedTimestamp
      : completedKind && timestamp != null && duration != null
      ? timestamp - duration
      : elapsed ?? timestamp
    return [{
      id: stringValue(span.span_id, span.spanId, span.event_id) || `${traceId}:${index}`,
      parentId: stringValue(span.parent_span_id, span.parentSpanId),
      kind: stringValue(span.kind) || 'event',
      phase: PHASE_ORDER.includes(phase) ? phase : 'unknown',
      title: stringValue(span.summary, span.kind) || 'Trace event',
      role: stringValue(span.role),
      provider: stringValue(span.provider),
      model: stringValue(span.model),
      status: (['queued', 'running', 'success', 'completed', 'error', 'cancelled', 'skipped'].includes(status)
        ? (status === 'completed' ? 'success' : status)
        : 'unknown') as TraceSpanStatus,
      startedAt: startedAt != null && Number.isFinite(startedAt) ? startedAt : undefined,
      recordedAt: elapsed ?? timestamp,
      endedAt,
      durationMs: duration,
      elapsedMs: elapsed,
      startedElapsedMs: startedElapsed,
      endedElapsedMs: unfinishedCall ? undefined : endedElapsed,
      seq: numberValue(span.seq),
      attemptIndex: numberValue(span.attempt_index, span.attemptIndex),
      logicalCallId: stringValue(span.logical_call_id, span.logicalCallId),
      physicalAttemptId: stringValue(span.physical_attempt_id, span.physicalAttemptId),
      summary: stringValue(span.summary),
      attrs: asRecord(span.attrs) || {},
      input: span.input,
      output: span.output,
      inputSeq: numberValue(span.input_seq, span.inputSeq),
      inputChars: numberValue(span.input_chars, span.inputChars),
      outputChars: numberValue(span.output_chars, span.outputChars),
      inputTruncated: span.input_truncated === true,
      outputTruncated: span.output_truncated === true,
      payloadRef: stringValue(span.payload_ref, span.payloadRef),
      usage: asRecord(span.usage) || undefined,
      toolName: stringValue(span.tool_name, span.toolName),
    }]
  })
  if (!spans.length) return null
  const phases: TracePhase[] = PHASE_ORDER.map(name => {
    const phaseSpans = spans.filter(span => span.phase === name)
    const starts = phaseSpans.map(span => span.startedAt).filter((n): n is number => n != null)
    const ends = phaseSpans.map(span => span.endedAt ?? (span.startedAt != null && span.durationMs != null ? span.startedAt + span.durationMs : undefined)).filter((n): n is number => n != null)
    return {
      name,
      label: PHASE_LABELS[name],
      status: (phaseSpans.some(span => span.status === 'error') ? 'error'
        : phaseSpans.some(span => span.status === 'running') ? 'running'
          : phaseSpans.some(span => span.status === 'success') ? 'success' : 'unknown') as TraceSpanStatus,
      spans: phaseSpans,
      durationMs: name !== 'context' && starts.length && ends.length ? Math.max(...ends) - Math.min(...starts) : undefined,
    }
  }).filter(phase => phase.spans.length)
  const status = stringValue(row.status) || 'unknown'
  return {
    traceId,
    runId: stringValue(row.run_id, row.runId),
    turnId: stringValue(row.turn_id, row.turnId),
    status: (['success', 'completed', 'error', 'running', 'cancelled', 'queued'].includes(status)
      ? (status === 'completed' ? 'success' : status)
      : 'unknown') as TraceSpanStatus,
    requestedMode: stringValue(row.requested_mode, row.requestedMode),
    effectiveMode: stringValue(row.effective_mode, row.effectiveMode),
    phases,
    spans,
    currentSeq: numberValue(row.current_seq, row.currentSeq),
    complete: row.complete === true,
  }
}

/** Normalize the bounded request/response rows returned by logs.trace_details. */
export function detailsFromApi(value: unknown): TraceDetails | null {
  const row = asRecord(value)
  if (!row) return null
  const traceId = stringValue(row.trace_id, row.traceId)
  if (!traceId) return null
  const rawRows = Array.isArray(row.rows) ? row.rows : []
  const rows: TraceSpan[] = rawRows.flatMap((item, index) => {
    const span = asRecord(item)
    if (!span) return []
    const phase = (stringValue(span.phase) || 'unknown') as TracePhaseName
    const status = stringValue(span.status) || 'unknown'
    const timestamp = numberValue(span.ts) ?? (typeof span.ts === 'string' ? Date.parse(span.ts) : undefined)
    const elapsed = numberValue(span.elapsed_ms, span.elapsedMs)
    const startedElapsed = numberValue(span.started_elapsed_ms, span.startedElapsedMs)
    const endedElapsed = numberValue(span.ended_elapsed_ms, span.endedElapsedMs)
    const startedTimestamp = numberValue(span.started_ts, span.startedAt)
      ?? (typeof span.started_ts === 'string' ? Date.parse(span.started_ts) : undefined)
    const unfinishedCall = ['llm_request', 'llm_progress', 'tool_request', 'approval_wait'].includes(stringValue(span.kind) || '')
    const duration = unfinishedCall ? undefined : numberValue(span.duration_ms, span.durationMs)
    const completedKind = ['llm_response', 'llm_error', 'tool_response', 'approval_resolved'].includes(stringValue(span.kind) || '')
    const completedEnd = completedKind ? (endedElapsed ?? elapsed ?? timestamp) : undefined
    const endedAt = completedKind ? completedEnd : unfinishedCall ? undefined : elapsed
    // See projectionFromApi above: use the paired request timestamp when
    // available, then fall back to the completion timestamp minus duration.
    const startedAt = unfinishedCall
      ? startedElapsed ?? (elapsed == null ? startedTimestamp : undefined) ?? elapsed ?? timestamp
      : completedKind && startedElapsed != null
      ? startedElapsed
      : completedKind && elapsed != null && duration != null
      ? elapsed - duration
      : completedKind && startedTimestamp != null && elapsed == null
      ? startedTimestamp
      : completedKind && timestamp != null && duration != null
      ? timestamp - duration
      : elapsed ?? timestamp
    return [{
      id: stringValue(span.id) || `${traceId}:detail:${index}`,
      parentId: stringValue(span.parent_id, span.parentId),
      kind: stringValue(span.kind) || 'event',
      phase: PHASE_ORDER.includes(phase) ? phase : 'unknown',
      title: stringValue(span.tool_name, span.kind) || 'Trace event',
      provider: stringValue(span.provider),
      model: stringValue(span.model),
      status: (['queued', 'running', 'success', 'error', 'cancelled', 'skipped'].includes(status) ? status : 'unknown') as TraceSpanStatus,
      startedAt,
      recordedAt: elapsed ?? timestamp,
      endedAt,
      durationMs: duration,
      elapsedMs: elapsed,
      startedElapsedMs: startedElapsed,
      endedElapsedMs: unfinishedCall ? undefined : endedElapsed,
      seq: numberValue(span.seq),
      attemptIndex: numberValue(span.attempt, span.attempt_index),
      logicalCallId: stringValue(span.call_id, span.logical_call_id),
      input: span.input,
      output: span.output,
      inputSeq: numberValue(span.input_seq, span.inputSeq),
      inputChars: numberValue(span.input_chars, span.inputChars),
      outputChars: numberValue(span.output_chars, span.outputChars),
      inputTruncated: span.input_truncated === true,
      outputTruncated: span.output_truncated === true,
      payloadRef: stringValue(span.payload_ref, span.payloadRef),
      usage: asRecord(span.usage) || undefined,
      toolName: stringValue(span.tool_name, span.toolName),
      summary: stringValue(span.stage, span.kind),
      attrs: {
        ...(asRecord(span.attrs) || {}),
        order_seq: numberValue(span.order_seq, asRecord(span.attrs)?.order_seq, span.seq),
      },
    }]
  })
  // The latest snapshot advances seq; its request sequence keeps the logical
  // call in the same position while progress is replaced by its final result.
  rows.sort((left, right) => (numberValue(left.attrs?.order_seq) ?? 0) - (numberValue(right.attrs?.order_seq) ?? 0))
  return {
    traceId,
    available: row.available === true,
    reason: stringValue(row.reason),
    rows,
    count: numberValue(row.count) || rows.length,
    total: numberValue(row.total) || rows.length,
    hasMore: row.has_more === true,
    clockOrigin: stringValue(row.clock_origin) === 'turn_runner_start' ? 'turn_runner_start' : 'logger_start',
  }
}
