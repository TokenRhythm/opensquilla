import type {
  ActivitySnapshotEntry,
  ActivitySnapshotV2,
  ChatMessage,
  ChatStreamTimelineItem,
} from '@/types/chat'
import type { ChatPart, InterruptResolution, StatusPart } from '@/types/parts'
import type { ReasoningBlock, ReasoningBlockStatus } from '@/types/turnlog'

const MAX_ENTRIES = 2048
const MAX_TIMESTAMP = 10_000_000_000_000
const TOOL_NAME = /^[A-Za-z0-9_.:-]{1,128}$/
const CHECKSUM = /^[0-9a-f]{64}$/
const PHASES: Readonly<Record<string, ReadonlySet<string>>> = {
  router: new Set(['decided']),
  state: new Set(['thinking', 'streaming', 'tool_calling']),
  provider: new Set(['requesting', 'reasoning', 'retry_wait', 'retrying', 'fallback']),
  write: new Set(['writing']),
}
const MAINTENANCE_STATES = new Set([
  'running', 'completed', 'skipped', 'stale', 'cancelled', 'failed',
])
const MAINTENANCE_REASONS = new Set(['within_budget', 'within_compaction_budget'])
const RESOLUTIONS = new Set([
  'approved', 'denied', 'expired', 'unavailable', 'replied',
])

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function safeText(value: unknown, maximum = 240): string | undefined {
  if (typeof value !== 'string') return undefined
  const normalized = value.trim()
  return normalized && normalized.length <= maximum ? normalized : undefined
}

function safeInteger(
  value: unknown,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): number | undefined {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= minimum
    && value <= maximum
    ? value
    : undefined
}

function onlyKeys(raw: Record<string, unknown>, allowed: readonly string[]): boolean {
  const allow = new Set(allowed)
  return Object.keys(raw).every(key => allow.has(key))
}

function parseCommon(
  raw: Record<string, unknown>,
  previousOrder: number,
  ids: Set<string>,
): { type: string; id: string; order: number } | undefined {
  const type = safeText(raw.type, 32)
  const id = safeText(raw.id)
  const order = safeInteger(raw.order, 1)
  if (!type || !id || order === undefined || order < previousOrder || ids.has(id)) {
    return undefined
  }
  return { type, id, order }
}

function parsePhase(
  raw: Record<string, unknown>,
  common: { type: string; id: string; order: number },
): ActivitySnapshotEntry | undefined {
  if (!onlyKeys(raw, [
    'type', 'id', 'order', 'kind', 'phase', 'at', 'ended_at', 'reason',
    'retry_after_ms', 'retry_attempt', 'retry_limit', 'round',
  ])) return undefined
  const kind = safeText(raw.kind, 32)
  const phase = safeText(raw.phase, 32)
  const at = safeInteger(raw.at, 1, MAX_TIMESTAMP)
  const endedAt = safeInteger(raw.ended_at, 1, MAX_TIMESTAMP)
  if (
    !kind || !phase || !PHASES[kind]?.has(phase)
    || at === undefined || endedAt === undefined || endedAt < at
  ) return undefined
  const entry: ActivitySnapshotEntry = {
    ...common,
    type: 'phase',
    kind,
    phase,
    at,
    ended_at: endedAt,
  }
  if (kind === 'provider') {
    if (raw.reason !== undefined) {
      if (raw.reason !== 'rate_limited') return undefined
      entry.reason = 'rate_limited'
    }
    for (const [field, maximum] of [
      ['retry_after_ms', 900_000],
      ['retry_attempt', 10_000],
      ['retry_limit', 10_000],
    ] as const) {
      if (raw[field] === undefined) continue
      const value = safeInteger(raw[field], 0, maximum)
      if (value === undefined) return undefined
      entry[field] = value
    }
    if (raw.round !== undefined) return undefined
  } else if (kind === 'write') {
    const round = safeInteger(raw.round, 1, 100_000)
    if (round === undefined) return undefined
    entry.round = round
    if (
      raw.reason !== undefined || raw.retry_after_ms !== undefined
      || raw.retry_attempt !== undefined || raw.retry_limit !== undefined
    ) return undefined
  } else if (
    raw.reason !== undefined || raw.retry_after_ms !== undefined
    || raw.retry_attempt !== undefined || raw.retry_limit !== undefined
    || raw.round !== undefined
  ) return undefined
  return entry
}

function parseReasoning(
  raw: Record<string, unknown>,
  common: { type: string; id: string; order: number },
): ActivitySnapshotEntry | undefined {
  if (!onlyKeys(raw, [
    'type', 'id', 'order', 'block_index', 'started_at', 'ended_at',
    'status', 'content_kind', 'text_start_utf16', 'text_end_utf16',
  ])) return undefined
  const blockIndex = safeInteger(raw.block_index, 0, 100_000)
  const startedAt = safeInteger(raw.started_at, 1, MAX_TIMESTAMP)
  const endedAt = safeInteger(raw.ended_at, 1, MAX_TIMESTAMP)
  const start = safeInteger(raw.text_start_utf16, 0, 100_000_000)
  const end = safeInteger(raw.text_end_utf16, 0, 100_000_000)
  const status = safeText(raw.status, 32)
  const contentKind = safeText(raw.content_kind, 32)
  if (
    blockIndex === undefined || startedAt === undefined || endedAt === undefined
    || endedAt < startedAt || start === undefined || end === undefined || end < start
    || !status || !['completed', 'interrupted', 'error'].includes(status)
    || !contentKind || !['reasoning', 'summary'].includes(contentKind)
  ) return undefined
  return {
    ...common,
    type: 'reasoning',
    block_index: blockIndex,
    started_at: startedAt,
    ended_at: endedAt,
    status,
    content_kind: contentKind,
    text_start_utf16: start,
    text_end_utf16: end,
  }
}

function parseSegment(
  raw: Record<string, unknown>,
  common: { type: string; id: string; order: number },
): ActivitySnapshotEntry | undefined {
  const segmentType = safeText(raw.segment_type, 32)
  if (segmentType === 'text') {
    if (!onlyKeys(raw, [
      'type', 'id', 'order', 'segment_type', 'text_index',
      'text_utf16_length', 'at', 'ended_at',
    ])) return undefined
    const textIndex = safeInteger(raw.text_index, 0, 100_000)
    const length = safeInteger(raw.text_utf16_length, 1, 100_000_000)
    const at = safeInteger(raw.at, 1, MAX_TIMESTAMP)
    const endedAt = safeInteger(raw.ended_at, 1, MAX_TIMESTAMP)
    if (
      textIndex === undefined || length === undefined || at === undefined
      || endedAt === undefined || endedAt < at
    ) return undefined
    return {
      ...common,
      type: 'segment',
      segment_type: 'text',
      text_index: textIndex,
      text_utf16_length: length,
      at,
      ended_at: endedAt,
    }
  }
  if (segmentType !== 'tool' || !onlyKeys(raw, [
    'type', 'id', 'order', 'segment_type', 'tool_use_id', 'name',
    'started_at', 'ended_at', 'is_error',
  ])) return undefined
  const toolUseId = safeText(raw.tool_use_id, 200)
  const name = safeText(raw.name, 128)
  const startedAt = safeInteger(raw.started_at, 1, MAX_TIMESTAMP)
  const endedAt = raw.ended_at === undefined
    ? undefined
    : safeInteger(raw.ended_at, 1, MAX_TIMESTAMP)
  if (
    !toolUseId || !name || !TOOL_NAME.test(name) || startedAt === undefined
    || (raw.ended_at !== undefined && endedAt === undefined)
    || (endedAt !== undefined && endedAt < startedAt)
    || (raw.is_error !== undefined && typeof raw.is_error !== 'boolean')
  ) return undefined
  return {
    ...common,
    type: 'segment',
    segment_type: 'tool',
    tool_use_id: toolUseId,
    name,
    started_at: startedAt,
    ...(endedAt !== undefined ? { ended_at: endedAt } : {}),
    ...(typeof raw.is_error === 'boolean' ? { is_error: raw.is_error } : {}),
  }
}

function parseMaintenance(
  raw: Record<string, unknown>,
  common: { type: string; id: string; order: number },
): ActivitySnapshotEntry | undefined {
  if (!onlyKeys(raw, [
    'type', 'id', 'order', 'maintenance_type', 'state', 'at',
    'ended_at', 'source', 'durability',
    'reason',
  ])) return undefined
  const state = safeText(raw.state, 32)
  const at = safeInteger(raw.at, 1, MAX_TIMESTAMP)
  const endedAt = safeInteger(raw.ended_at, 1, MAX_TIMESTAMP)
  if (
    raw.maintenance_type !== 'context_compaction' || !state
    || !MAINTENANCE_STATES.has(state) || at === undefined
    || endedAt === undefined || endedAt < at
  ) return undefined
  const entry: ActivitySnapshotEntry = {
    ...common,
    type: 'maintenance',
    maintenance_type: 'context_compaction',
    state,
    at,
    ended_at: endedAt,
  }
  for (const field of ['source', 'durability'] as const) {
    if (raw[field] === undefined) continue
    const value = safeText(raw[field], 64)
    if (!value) return undefined
    entry[field] = value
  }
  if (raw.reason !== undefined) {
    const reason = safeText(raw.reason, 64)
    if (!reason || !MAINTENANCE_REASONS.has(reason)) return undefined
    entry.reason = reason
  }
  return entry
}

function parseInterrupt(
  raw: Record<string, unknown>,
  common: { type: string; id: string; order: number },
): ActivitySnapshotEntry | undefined {
  if (!onlyKeys(raw, [
    'type', 'id', 'order', 'interrupt_type', 'reference_id', 'tool_use_id',
    'namespace', 'tool_name', 'approval_kind', 'resolution',
    'started_at', 'ended_at',
  ])) return undefined
  const interruptType = safeText(raw.interrupt_type, 32)
  const referenceId = safeText(raw.reference_id, 200)
  const resolution = safeText(raw.resolution, 32)
  const startedAt = safeInteger(raw.started_at, 1, MAX_TIMESTAMP)
  const endedAt = safeInteger(raw.ended_at, 1, MAX_TIMESTAMP)
  if (
    !interruptType || !['approval', 'clarify'].includes(interruptType)
    || !referenceId || !resolution || !RESOLUTIONS.has(resolution)
    || startedAt === undefined || endedAt === undefined || endedAt < startedAt
  ) return undefined
  const entry: ActivitySnapshotEntry = {
    ...common,
    type: 'interrupt',
    interrupt_type: interruptType,
    reference_id: referenceId,
    resolution,
    started_at: startedAt,
    ended_at: endedAt,
  }
  if (interruptType === 'approval') {
    const namespace = safeText(raw.namespace, 64)
    const toolName = raw.tool_name === undefined ? undefined : safeText(raw.tool_name, 128)
    const approvalKind = raw.approval_kind === undefined
      ? undefined
      : safeText(raw.approval_kind, 128)
    if (
      !namespace || resolution === 'replied' || raw.tool_use_id !== undefined
      || (raw.tool_name !== undefined && (!toolName || !TOOL_NAME.test(toolName)))
      || (raw.approval_kind !== undefined && !approvalKind)
    ) return undefined
    entry.namespace = namespace
    if (toolName) entry.tool_name = toolName
    if (approvalKind) entry.approval_kind = approvalKind
  } else {
    const toolUseId = safeText(raw.tool_use_id, 200)
    if (
      resolution !== 'replied' || !toolUseId || raw.namespace !== undefined
      || raw.tool_name !== undefined || raw.approval_kind !== undefined
    ) return undefined
    entry.tool_use_id = toolUseId
  }
  return entry
}

export function normalizeActivitySnapshot(
  value: unknown,
  expectedTurnId = '',
  expectedTaskId = '',
): ActivitySnapshotV2 | undefined {
  const raw = record(value)
  if (
    !raw || !onlyKeys(raw, [
      'version', 'task_id', 'turn_id', 'complete',
      'reasoning_utf16_length', 'entries', 'checksum',
    ])
    || raw.version !== 2 || typeof raw.complete !== 'boolean'
    || !Array.isArray(raw.entries) || !raw.entries.length
    || raw.entries.length > MAX_ENTRIES
  ) return undefined
  const taskId = safeText(raw.task_id)
  const turnId = safeText(raw.turn_id)
  if (
    !taskId || !turnId || (expectedTurnId && turnId !== expectedTurnId)
    || (expectedTaskId && taskId !== expectedTaskId)
  ) return undefined
  const suppliedChecksum = raw.checksum === undefined ? undefined : safeText(raw.checksum, 64)
  if (raw.checksum !== undefined && (!suppliedChecksum || !CHECKSUM.test(suppliedChecksum))) {
    return undefined
  }

  const entries: ActivitySnapshotEntry[] = []
  const ids = new Set<string>()
  const textIndices: number[] = []
  const reasoningSpans: Array<[number, number]> = []
  let previousOrder = 0
  for (const candidate of raw.entries) {
    const entryRaw = record(candidate)
    if (!entryRaw) return undefined
    const common = parseCommon(entryRaw, previousOrder, ids)
    if (!common) return undefined
    let entry: ActivitySnapshotEntry | undefined
    if (common.type === 'phase') entry = parsePhase(entryRaw, common)
    else if (common.type === 'reasoning') entry = parseReasoning(entryRaw, common)
    else if (common.type === 'segment') entry = parseSegment(entryRaw, common)
    else if (common.type === 'interrupt') entry = parseInterrupt(entryRaw, common)
    else if (common.type === 'maintenance') entry = parseMaintenance(entryRaw, common)
    if (!entry) return undefined
    ids.add(common.id)
    previousOrder = common.order
    entries.push(entry)
    if (entry.type === 'segment' && entry.segment_type === 'text') {
      textIndices.push(Number(entry.text_index))
    }
    if (entry.type === 'reasoning') {
      reasoningSpans.push([
        Number(entry.text_start_utf16),
        Number(entry.text_end_utf16),
      ])
    }
  }
  if (
    textIndices.length
    && [...textIndices].sort((a, b) => a - b)
      .some((value, index) => value !== index)
  ) return undefined
  const reasoningUtf16Length = safeInteger(
    raw.reasoning_utf16_length,
    0,
    100_000_000,
  )
  if (reasoningUtf16Length === undefined) return undefined
  let cursor = 0
  for (const [index, [start, end]] of [...reasoningSpans]
    .sort((a, b) => a[0] - b[0]).entries()) {
    const expectedStart = index === 0 ? cursor : cursor + 1
    if (start !== expectedStart) return undefined
    cursor = end
  }
  if (cursor !== reasoningUtf16Length) return undefined
  return {
    version: 2,
    taskId,
    turnId,
    complete: raw.complete,
    reasoningUtf16Length,
    entries,
    ...(suppliedChecksum ? { checksum: suppliedChecksum } : {}),
  }
}

function phaseStatus(entry: ActivitySnapshotEntry): StatusPart | undefined {
  const kind = safeText(entry.kind, 32)
  const phase = safeText(entry.phase, 32)
  const at = safeInteger(entry.at, 1)
  const endedAt = safeInteger(entry.ended_at, 1)
  if (!kind || !phase || at === undefined || endedAt === undefined) return undefined
  const common = {
    at,
    endedAt,
    activityOrder: entry.order,
    durability: 'durable',
  }
  if (kind === 'router') return { ...common, action: 'router:decided', label: 'Selecting model' }
  if (kind === 'state' && phase === 'thinking') {
    return { ...common, action: 'Planning next step', label: 'Planning next step' }
  }
  if (kind === 'state' && phase === 'streaming') {
    return { ...common, action: 'Model is generating', label: 'Model is generating' }
  }
  if (kind === 'state' && phase === 'tool_calling') {
    return { ...common, action: 'Preparing tool call', label: 'Preparing tool call' }
  }
  if (kind === 'write') {
    const round = safeInteger(entry.round, 1) ?? 1
    return { ...common, action: `write:${round}`, label: 'Writing reply' }
  }
  if (kind !== 'provider') return undefined
  if (phase === 'requesting') {
    return { ...common, action: 'provider:requesting', label: 'Waiting for model' }
  }
  if (phase === 'reasoning') {
    return { ...common, action: 'provider:reasoning', label: 'Thinking deeply' }
  }
  const seconds = Math.ceil((safeInteger(entry.retry_after_ms) ?? 0) / 1000)
  if (phase === 'retry_wait' && entry.reason === 'rate_limited') {
    return {
      ...common,
      action: `provider:rate_limited:${seconds}`,
      label: `Rate limited · ${seconds}s`,
    }
  }
  if (phase === 'retry_wait') {
    return {
      ...common,
      action: `provider:retry_wait:${seconds}`,
      label: `Waiting to retry · ${seconds}s`,
    }
  }
  if (phase === 'retrying') {
    const attempt = safeInteger(entry.retry_attempt) ?? 0
    const limit = safeInteger(entry.retry_limit) ?? 0
    return {
      ...common,
      action: `provider:retrying:${attempt}:${limit}`,
      label: `Retrying ${attempt}/${limit}`,
    }
  }
  if (phase === 'fallback') {
    return { ...common, action: 'provider:fallback', label: 'Switching to backup model' }
  }
  return undefined
}

export function activityStatusHistory(snapshot: ActivitySnapshotV2): StatusPart[] {
  return snapshot.entries.flatMap((entry): StatusPart[] => {
    if (entry.type === 'phase') {
      const status = phaseStatus(entry)
      return status ? [status] : []
    }
    if (entry.type !== 'maintenance') return []
    const at = safeInteger(entry.at, 1)
    const endedAt = safeInteger(entry.ended_at, 1)
    const state = safeText(entry.state, 32)
    if (
      at === undefined || endedAt === undefined || !state
      || !MAINTENANCE_STATES.has(state)
    ) return []
    return [{
      action: 'context_compaction',
      label: 'Organizing context',
      at,
      endedAt,
      activityOrder: entry.order,
      id: entry.id,
      category: 'maintenance',
      state: state as StatusPart['state'],
      source: safeText(entry.source, 64),
      durability: safeText(entry.durability, 64),
      reason: safeText(entry.reason, 64),
    }]
  })
}

export function activityReasoningBlocks(
  snapshot: ActivitySnapshotV2,
  reasoningText: string,
): ReasoningBlock[] | undefined {
  if ((snapshot.reasoningUtf16Length ?? 0) !== reasoningText.length) return undefined
  const entries = snapshot.entries.filter(entry => entry.type === 'reasoning')
  if (!entries.length) return reasoningText.length === 0 ? [] : undefined
  const blocks: ReasoningBlock[] = []
  let previousEnd = 0
  for (const entry of entries) {
    const start = safeInteger(entry.text_start_utf16)
    const end = safeInteger(entry.text_end_utf16)
    const startedAt = safeInteger(entry.started_at, 1)
    const endedAt = safeInteger(entry.ended_at, 1)
    const rawStatus = safeText(entry.status, 32)
    if (
      start === undefined || end === undefined || end < start
      || end > reasoningText.length || startedAt === undefined
      || endedAt === undefined || endedAt < startedAt || !rawStatus
    ) return undefined
    const separator = reasoningText.slice(previousEnd, start)
    if (blocks.length === 0 ? separator !== '' : separator !== '\n') return undefined
    blocks.push({
      id: entry.id,
      index: safeInteger(entry.block_index) ?? blocks.length,
      text: reasoningText.slice(start, end),
      status: rawStatus as ReasoningBlockStatus,
      startedAt,
      endedAt,
      contentKind: entry.content_kind === 'summary' ? 'summary' : 'reasoning',
      activityOrder: entry.order,
    })
    previousEnd = end
  }
  return blocks
}

function transcriptTextSegments(message: ChatMessage): string[] {
  if (Array.isArray(message.timeline) && message.timeline.length) {
    const timelineTexts = message.timeline.flatMap(segment => {
      if (segment?.type !== 'text') return []
      const value = segment.raw ?? segment.text
      return typeof value === 'string' && value ? [value] : []
    })
    if (timelineTexts.length) return timelineTexts
  }
  const persisted = Array.isArray(message.tool_calls) ? message.tool_calls : []
  const segmentTexts = persisted.flatMap(segment => {
    if (segment?.type !== 'text') return []
    const value = segment.text ?? segment.raw
    return typeof value === 'string' && value ? [value] : []
  })
  return segmentTexts.length ? segmentTexts : message.text ? [message.text] : []
}

function transcriptTools(message: ChatMessage): Map<string, string> | undefined {
  const tools = new Map<string, string>()
  for (const segment of message.tool_calls ?? []) {
    const type = String(segment?.type || '')
    if (type && !['tool_use', 'tool_result'].includes(type)) continue
    const name = safeText(segment.name ?? segment.tool_name, 128)
    if (!name || name === 'router_control') continue
    const id = safeText(
      segment.tool_use_id ?? segment.toolId ?? segment.id,
      200,
    )
    if (!id || !TOOL_NAME.test(name)) return undefined
    const prior = tools.get(id)
    if (prior && prior !== name) return undefined
    tools.set(id, name)
  }
  return tools
}

function jsonRecord(value: unknown): Record<string, unknown> | undefined {
  const direct = record(value)
  if (direct) return direct
  if (typeof value !== 'string' || value.length > 2_000_000) return undefined
  try {
    return record(JSON.parse(value))
  } catch {
    return undefined
  }
}

function transcriptClarifyIds(message: ChatMessage): Set<string> {
  const ids = new Set<string>()
  for (const segment of message.tool_calls ?? []) {
    const value = jsonRecord(segment.result ?? segment.content ?? segment.output)
    if (value?.kind !== 'user_input') continue
    const id = safeText(value.request_id ?? value.requestId, 200)
    if (id) ids.add(id)
  }
  return ids
}

/** Validate every transcript reference before allowing a complete v2 to render. */
export function activitySnapshotMatchesMessage(
  snapshot: ActivitySnapshotV2,
  message: ChatMessage,
): boolean {
  if (!snapshot.complete) return false
  if (activityReasoningBlocks(snapshot, message.reasoning?.text ?? '') === undefined) {
    return false
  }
  const textEntries = snapshot.entries
    .filter(entry => entry.type === 'segment' && entry.segment_type === 'text')
    .sort((a, b) => Number(a.text_index) - Number(b.text_index))
  const transcriptTexts = transcriptTextSegments(message)
  if (
    textEntries.length !== transcriptTexts.length
    || textEntries.some((entry, index) => (
      Number(entry.text_utf16_length) !== transcriptTexts[index]!.length
    ))
  ) return false

  const transcriptToolMap = transcriptTools(message)
  if (!transcriptToolMap) return false
  const snapshotTools = snapshot.entries
    .filter(entry => entry.type === 'segment' && entry.segment_type === 'tool')
  if (snapshotTools.length !== transcriptToolMap.size) return false
  for (const entry of snapshotTools) {
    if (transcriptToolMap.get(String(entry.tool_use_id)) !== entry.name) return false
  }
  const clarifyIds = transcriptClarifyIds(message)
  return snapshot.entries.every(entry => (
    entry.type !== 'interrupt'
    || entry.interrupt_type !== 'clarify'
    || clarifyIds.has(String(entry.reference_id))
  ))
}

export function applyActivityOrdersToTimeline(
  items: ChatStreamTimelineItem[],
  snapshot: ActivitySnapshotV2 | undefined,
): ChatStreamTimelineItem[] {
  if (!snapshot?.complete) return items
  const textOrder = new Map<number, number>()
  const toolOrder = new Map<string, number>()
  const interruptOrder = new Map<string, number>()
  for (const entry of snapshot.entries) {
    if (entry.type === 'segment' && entry.segment_type === 'text') {
      textOrder.set(Number(entry.text_index), entry.order)
    } else if (entry.type === 'segment' && entry.segment_type === 'tool') {
      toolOrder.set(String(entry.tool_use_id), entry.order)
    } else if (entry.type === 'interrupt') {
      interruptOrder.set(String(entry.reference_id), entry.order)
    }
  }
  let textIndex = 0
  return items.map((item): ChatStreamTimelineItem => {
    if (item.type === 'text') {
      const activityOrder = textOrder.get(textIndex++)
      return activityOrder === undefined ? item : { ...item, activityOrder }
    }
    if (item.type === 'interrupt') {
      const activityOrder = interruptOrder.get(item.approvalId)
      return activityOrder === undefined ? item : { ...item, activityOrder }
    }
    const calls = item.group.calls.map(call => {
      const activityOrder = toolOrder.get(call.toolId)
      return activityOrder === undefined ? call : { ...call, activityOrder }
    })
    const orders = calls.flatMap(call => call.activityOrder === undefined ? [] : [call.activityOrder])
    const activityOrder = orders.length ? Math.min(...orders) : undefined
    return {
      ...item,
      ...(activityOrder !== undefined ? { activityOrder } : {}),
      group: {
        ...item.group,
        calls,
        ...(activityOrder !== undefined ? { activityOrder } : {}),
      },
    }
  })
}

function partReferenceId(part: Extract<ChatPart, { type: 'interrupt' }>): string {
  return part.approval?.approvalId || part.clarify?.requestId || ''
}

/** Add resolved interrupt rows at their original order without persisting details. */
export function restoreActivityInterruptTimeline(
  items: ChatStreamTimelineItem[],
  parts: ChatPart[],
  snapshot: ActivitySnapshotV2 | undefined,
  ownerKey: string,
): { items: ChatStreamTimelineItem[]; parts: ChatPart[] } {
  if (!snapshot?.complete) return { items, parts }
  const nextItems = [...items]
  const nextParts = [...parts]
  const existingItems = new Set(
    nextItems.flatMap(item => item.type === 'interrupt' ? [item.approvalId] : []),
  )
  const interruptParts = new Map(
    nextParts.flatMap(part => (
      part.type === 'interrupt' && partReferenceId(part)
        ? [[partReferenceId(part), part] as const]
        : []
    )),
  )
  for (const entry of snapshot.entries) {
    if (entry.type !== 'interrupt') continue
    const referenceId = String(entry.reference_id)
    let part = interruptParts.get(referenceId)
    if (!part && entry.interrupt_type === 'approval') {
      const toolName = String(
        entry.tool_name || entry.approval_kind || entry.namespace || 'approval',
      )
      part = {
        type: 'interrupt',
        interruptKind: 'approval',
        approval: {
          approvalId: referenceId,
          namespace: String(entry.namespace || 'exec'),
          toolName,
          command: '',
          approvalKind: String(entry.approval_kind || ''),
          args: null,
          warning: '',
          agent: '',
          sessionKey: '',
          deadline: 0,
        },
        resolution: entry.resolution as InterruptResolution,
        busy: false,
        error: '',
        key: `${ownerKey}:interrupt:${referenceId}`,
      }
      nextParts.push(part)
      interruptParts.set(referenceId, part)
    }
    if (!part || existingItems.has(referenceId)) continue
    nextItems.push({
      type: 'interrupt',
      key: part.key,
      approvalId: referenceId,
      part,
      activityOrder: entry.order,
    })
    existingItems.add(referenceId)
  }
  return { items: nextItems, parts: nextParts }
}
