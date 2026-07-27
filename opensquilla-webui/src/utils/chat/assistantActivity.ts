import type {
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import type { ChatPart, StatusPart } from '@/types/parts'

type TextPart = Extract<ChatPart, { type: 'text' }>

export type AssistantActivityLifecycle =
  | 'working'
  | 'answering'
  | 'settled'
  | 'interrupted'
  | 'failed'

export type AssistantActivityClusterState =
  | 'complete'
  | 'running'
  | 'failed'
  | 'pending'

export type AssistantActivityLifecycleCode =
  | 'chat.activity.lifecycle.working'
  | 'chat.activity.lifecycle.answering'
  | 'chat.activity.lifecycle.settled'
  | 'chat.activity.lifecycle.interrupted'
  | 'chat.activity.lifecycle.failed'

export type AssistantActivityPurposeBaseCode =
  | 'chat.activity.purpose.discover'
  | 'chat.activity.purpose.search'
  | 'chat.activity.purpose.read'
  | 'chat.activity.purpose.inspect'
  | 'chat.activity.purpose.change'
  | 'chat.activity.purpose.run'
  | 'chat.activity.purpose.create'
  | 'chat.activity.purpose.recall'
  | 'chat.activity.purpose.use'

export type AssistantActivityPurposeRunningCode =
  | 'chat.activity.purposeRunning.discover'
  | 'chat.activity.purposeRunning.search'
  | 'chat.activity.purposeRunning.read'
  | 'chat.activity.purposeRunning.inspect'
  | 'chat.activity.purposeRunning.change'
  | 'chat.activity.purposeRunning.run'
  | 'chat.activity.purposeRunning.create'
  | 'chat.activity.purposeRunning.recall'
  | 'chat.activity.purposeRunning.use'

export type AssistantActivityPurposeCode =
  | AssistantActivityPurposeBaseCode
  | AssistantActivityPurposeRunningCode

export type AssistantActivityFootprintCode =
  | 'chat.activity.footprint.web'
  | 'chat.activity.footprint.files'
  | 'chat.activity.footprint.fileOperations'
  | 'chat.activity.footprint.commands'
  | 'chat.activity.footprint.artifacts'
  | 'chat.activity.footprint.memory'
  | 'chat.activity.footprint.tools'

export type AssistantActivityMoreCode = 'chat.activity.more'

export type AssistantActivityCodeParams = Readonly<Record<string, string | number>>

export interface AssistantActivityCodeDescriptor<Code extends string> {
  code: Code
  params: AssistantActivityCodeParams
}

export interface AssistantActivityCodeSummary<Code extends string> {
  /**
   * At most two semantic labels, in the order they first appeared.
   */
  codes: AssistantActivityCodeDescriptor<Code>[]
  /**
   * Number of distinct labels omitted from `codes`, not the number of calls.
   */
  remainingCount: number
  /**
   * Overflow segment whose `count` param is the summed call count of the
   * omitted labels, so it shares a unit with the visible `codes` segments.
   */
  remaining: AssistantActivityCodeDescriptor<AssistantActivityMoreCode> | null
}

export interface AssistantActivityCluster {
  /**
   * Stable for the lifetime of the first call in the cluster. It never embeds
   * tool input, output, command text, or file paths.
   */
  key: string
  purpose: AssistantActivityCodeDescriptor<AssistantActivityPurposeCode>
  footprint: AssistantActivityCodeDescriptor<AssistantActivityFootprintCode>
  state: AssistantActivityClusterState
  isCurrent: boolean
  isFailure: boolean
  callCount: number
  /**
   * Original calls are retained solely for an explicitly expanded detail view.
   */
  calls: ChatToolCallRenderItem[]
}

export type AssistantActivityStatusCode =
  | AssistantActivityLifecycleCode
  | AssistantActivityPurposeCode

export interface AssistantActivityStatusStep {
  key: string
  label: AssistantActivityCodeDescriptor<AssistantActivityStatusCode>
  at: number
  isCurrent: boolean
}

export interface AssistantActivityTimelineProjection {
  lifecycle: AssistantActivityLifecycle
  lifecycleLabel: AssistantActivityCodeDescriptor<AssistantActivityLifecycleCode>
  activityClusters: AssistantActivityCluster[]
  purposeSummary: AssistantActivityCodeSummary<AssistantActivityPurposeCode>
  footprintSummary: AssistantActivityCodeSummary<AssistantActivityFootprintCode>
  currentClusterKey: string | null
  /**
   * Safe phase labels derived from structured status action codes. Raw status
   * labels are deliberately excluded because they may contain paths or tool
   * arguments.
   */
  statusSteps: AssistantActivityStatusStep[]
}

export interface ProjectAssistantActivityOptions {
  lifecycle?: AssistantActivityLifecycle
  statusHistory?: readonly StatusPart[]
}

export interface LiveAssistantTimelineSplit {
  activityItems: ChatStreamTimelineItem[]
  answerItem: Extract<ChatStreamTimelineItem, { type: 'text' }> | null
}

export interface AssistantActivityProjection extends AssistantActivityTimelineProjection {
  /**
   * Whether the message can be rendered as a compact activity disclosure plus
   * one canonical answer. False is the compatibility path for older history
   * rows that have timeline text but no authoritative message.text.
   */
  canSeparateActivity: boolean
  activityItems: ChatStreamTimelineItem[]
  answerPart: TextPart | null
  toolCount: number
  failureCount: number
}

interface ActivitySemantic {
  purpose: AssistantActivityPurposeBaseCode
  footprintKind: 'web' | 'file' | 'command' | 'artifact' | 'memory' | 'tool'
}

const LIFECYCLE_CODES: Record<AssistantActivityLifecycle, AssistantActivityLifecycleCode> = {
  working: 'chat.activity.lifecycle.working',
  answering: 'chat.activity.lifecycle.answering',
  settled: 'chat.activity.lifecycle.settled',
  interrupted: 'chat.activity.lifecycle.interrupted',
  failed: 'chat.activity.lifecycle.failed',
}

const DEFAULT_SEMANTIC: ActivitySemantic = {
  purpose: 'chat.activity.purpose.use',
  footprintKind: 'tool',
}

const DISCOVER_TOOLS = new Set(['web_discover'])
const SEARCH_TOOLS = new Set(['web_search', 'search_query', 'image_query'])
const WEB_READ_TOOLS = new Set(['web_fetch', 'open_url', 'http_request'])
const FILE_INSPECT_TOOLS = new Set([
  'read_file',
  'read_source',
  'read_spreadsheet',
  'list_dir',
  'list_directory',
  'glob_search',
  'grep_search',
])
const FILE_CHANGE_TOOLS = new Set([
  'write_file',
  'write_scratch',
  'create_file',
  'create_source',
  'edit_file',
  'edit_source',
  'apply_patch',
])
const COMMAND_TOOLS = new Set([
  'exec',
  'exec_command',
  'execute_code',
  'bash',
  'bash_exec',
  'shell',
  'python',
  'python_exec',
  'py',
])
const ARTIFACT_TOOLS = new Set(['publish_artifact'])
const MEMORY_TOOLS = new Set(['memory_search', 'search_memory'])
const FILE_TARGET_KEYS = [
  'path',
  'file_path',
  'filePath',
  'filename',
  'target_path',
  'targetPath',
] as const

// Present-tense counterparts for a cluster that is still in flight. Settled
// clusters keep the past-tense purpose codes.
const RUNNING_PURPOSE_CODES: Readonly<
  Record<AssistantActivityPurposeBaseCode, AssistantActivityPurposeRunningCode>
> = {
  'chat.activity.purpose.discover': 'chat.activity.purposeRunning.discover',
  'chat.activity.purpose.search': 'chat.activity.purposeRunning.search',
  'chat.activity.purpose.read': 'chat.activity.purposeRunning.read',
  'chat.activity.purpose.inspect': 'chat.activity.purposeRunning.inspect',
  'chat.activity.purpose.change': 'chat.activity.purposeRunning.change',
  'chat.activity.purpose.run': 'chat.activity.purposeRunning.run',
  'chat.activity.purpose.create': 'chat.activity.purposeRunning.create',
  'chat.activity.purpose.recall': 'chat.activity.purposeRunning.recall',
  'chat.activity.purpose.use': 'chat.activity.purposeRunning.use',
}

const STATUS_PURPOSE_CODES: Readonly<Record<string, AssistantActivityPurposeCode>> = {
  discover: 'chat.activity.purpose.discover',
  search: 'chat.activity.purpose.search',
  read: 'chat.activity.purpose.read',
  inspect: 'chat.activity.purpose.inspect',
  change: 'chat.activity.purpose.change',
  edit: 'chat.activity.purpose.change',
  run: 'chat.activity.purpose.run',
  create: 'chat.activity.purpose.create',
  recall: 'chat.activity.purpose.recall',
}

/**
 * Treat only the current trailing text segment as an answer candidate. A later
 * tool append makes that text chronological activity again without mutating
 * the append-only timeline.
 */
export function splitLiveAssistantTimeline(
  timeline: ChatStreamTimelineItem[],
): LiveAssistantTimelineSplit {
  const last = timeline[timeline.length - 1]
  if (
    !last
    || last.type !== 'text'
    || (!String(last.rawText || '').trim() && !String(last.html || '').trim())
  ) {
    return { activityItems: timeline.slice(), answerItem: null }
  }
  return {
    activityItems: timeline.slice(0, -1),
    answerItem: { ...last },
  }
}

/**
 * Preserve narration that became part of the work chronology because another
 * tool ran after it. Any text before the last tool is process narration, while
 * trailing text remains a streamed answer snapshot whose authoritative
 * replacement is `message.text`.
 */
function separatedActivityItems(
  timeline: ChatStreamTimelineItem[],
  canonicalAnswer: string,
): ChatStreamTimelineItem[] {
  const normalizedAnswer = canonicalAnswer.trim().replace(/\s+/g, ' ')
  let lastToolIndex = -1
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    if (timeline[index]?.type === 'tool-group') {
      lastToolIndex = index
      break
    }
  }

  return timeline.filter((item, index) => {
    if (item.type === 'tool-group') return true
    if (index >= lastToolIndex) return false
    const rawText = String(item.rawText || '').trim()
    const html = String(item.html || '').trim()
    if (!rawText && !html) return false

    // A streamed fragment can precede a tool yet still be part of the
    // authoritative final answer. Do not render it twice. Distinct candidate
    // narration remains visible inside the activity chronology.
    const normalizedText = rawText.replace(/\s+/g, ' ')
    return !normalizedText || !normalizedAnswer.includes(normalizedText)
  })
}

function codeDescriptor<Code extends string>(
  code: Code,
  params: AssistantActivityCodeParams = {},
): AssistantActivityCodeDescriptor<Code> {
  return { code, params }
}

function activityToolName(name: string): string {
  const normalized = String(name || '')
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .replace(/-/g, '_')
  const namespaced = normalized.split(/__|[.:/]/).filter(Boolean)
  return namespaced[namespaced.length - 1] || ''
}

function callSemantic(call: ChatToolCallRenderItem): ActivitySemantic {
  const name = activityToolName(call.name)
  if (DISCOVER_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.discover', footprintKind: 'web' }
  }
  if (SEARCH_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.search', footprintKind: 'web' }
  }
  if (WEB_READ_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.read', footprintKind: 'web' }
  }
  if (FILE_INSPECT_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.inspect', footprintKind: 'file' }
  }
  if (FILE_CHANGE_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.change', footprintKind: 'file' }
  }
  if (COMMAND_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.run', footprintKind: 'command' }
  }
  if (ARTIFACT_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.create', footprintKind: 'artifact' }
  }
  if (MEMORY_TOOLS.has(name)) {
    return { purpose: 'chat.activity.purpose.recall', footprintKind: 'memory' }
  }
  return DEFAULT_SEMANTIC
}

function structuredFileTarget(call: ChatToolCallRenderItem): string | null {
  const raw = String(call.inputRaw || '').trim()
  if (!raw.startsWith('{')) return null
  try {
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    for (const key of FILE_TARGET_KEYS) {
      const value = (parsed as Record<string, unknown>)[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
  } catch {
    // Unstructured input is intentionally counted as an operation rather than
    // guessed to be a file target.
  }
  return null
}

function footprintDescriptor(
  semantic: ActivitySemantic,
  calls: ChatToolCallRenderItem[],
): AssistantActivityCodeDescriptor<AssistantActivityFootprintCode> {
  if (semantic.footprintKind === 'file') {
    const targets = calls.map(structuredFileTarget)
    if (targets.every((target): target is string => Boolean(target))) {
      return codeDescriptor('chat.activity.footprint.files', {
        count: new Set(targets).size,
      })
    }
    return codeDescriptor('chat.activity.footprint.fileOperations', {
      count: calls.length,
    })
  }

  const code: AssistantActivityFootprintCode =
    semantic.footprintKind === 'web'
      ? 'chat.activity.footprint.web'
      : semantic.footprintKind === 'command'
        ? 'chat.activity.footprint.commands'
        : semantic.footprintKind === 'artifact'
          ? 'chat.activity.footprint.artifacts'
          : semantic.footprintKind === 'memory'
            ? 'chat.activity.footprint.memory'
            : 'chat.activity.footprint.tools'
  return codeDescriptor(code, { count: calls.length })
}

function callState(call: ChatToolCallRenderItem): AssistantActivityClusterState {
  if (call.isError || call.status === 'error') return 'failed'
  if (call.isRunning) return 'running'
  if (call.status === 'success') return 'complete'
  return 'pending'
}

function stableHash(value: string): string {
  let hash = 0x811c9dc5
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 0x01000193)
  }
  return (hash >>> 0).toString(36)
}

function clusterKey(
  semantic: ActivitySemantic,
  firstCall: ChatToolCallRenderItem,
): string {
  const identity = firstCall.toolId || firstCall.renderKey
  return `activity-cluster:${stableHash(
    `${semantic.purpose}\u001f${semantic.footprintKind}\u001f${identity}`,
  )}`
}

function makeCluster(
  call: ChatToolCallRenderItem,
  semantic: ActivitySemantic,
  state: AssistantActivityClusterState,
  lifecycle: AssistantActivityLifecycle,
): AssistantActivityCluster {
  const isCurrentLifecycle = lifecycle === 'working' || lifecycle === 'answering'
  const isCurrent = isCurrentLifecycle && (state === 'running' || state === 'pending')
  return {
    key: clusterKey(semantic, call),
    purpose: codeDescriptor(
      isCurrent ? RUNNING_PURPOSE_CODES[semantic.purpose] : semantic.purpose,
      { count: 1 },
    ),
    footprint: footprintDescriptor(semantic, [call]),
    state,
    isCurrent,
    isFailure: state === 'failed',
    callCount: 1,
    calls: [call],
  }
}

function appendCall(
  cluster: AssistantActivityCluster,
  call: ChatToolCallRenderItem,
): void {
  cluster.calls.push(call)
  cluster.callCount += 1
  cluster.purpose = codeDescriptor(cluster.purpose.code, { count: cluster.callCount })
  cluster.footprint = footprintDescriptor(callSemantic(cluster.calls[0]), cluster.calls)
}

function summarizeCodes<Code extends string>(
  clusters: AssistantActivityCluster[],
  selectCode: (cluster: AssistantActivityCluster) => Code,
  selectCount: (cluster: AssistantActivityCluster) => number,
): AssistantActivityCodeSummary<Code> {
  const counts = new Map<Code, number>()
  for (const cluster of clusters) {
    const code = selectCode(cluster)
    counts.set(code, (counts.get(code) ?? 0) + selectCount(cluster))
  }

  const descriptors = [...counts].map(([code, count]) => codeDescriptor(code, { count }))
  const codes = descriptors.slice(0, 2)
  const omitted = descriptors.slice(codes.length)
  // The visible segments render call counts, so the overflow segment must be
  // denominated in calls too; the count of omitted kinds stays available as
  // `remainingCount` for callers that need it.
  const omittedCallCount = omitted.reduce(
    (total, descriptor) => total + descriptorCount(descriptor),
    0,
  )
  return {
    codes,
    remainingCount: omitted.length,
    remaining: omitted.length > 0
      ? codeDescriptor('chat.activity.more', { count: omittedCallCount })
      : null,
  }
}

function descriptorCount(
  descriptor: AssistantActivityCodeDescriptor<string>,
): number {
  const count = Number(descriptor.params.count ?? 0)
  return Number.isFinite(count) && count > 0 ? count : 0
}

function statusLabelFor(
  entry: StatusPart,
  clusters: AssistantActivityCluster[],
): AssistantActivityCodeDescriptor<AssistantActivityStatusCode> | null {
  const action = String(entry.action || '').trim()
  const normalized = action.toLowerCase()
  if (normalized.startsWith('tool:')) {
    const toolId = action.slice(action.indexOf(':') + 1)
    const cluster = clusters.find(candidate =>
      candidate.calls.some(call => call.toolId === toolId || call.renderKey === toolId),
    )
    // A matching tool cluster already carries the same phase with richer,
    // expandable details. Keep only unmatched tool phases as a generic,
    // non-leaking fallback.
    return cluster ? null : codeDescriptor('chat.activity.purpose.use')
  }
  if (normalized.startsWith('write:') || normalized === 'writing reply') {
    return codeDescriptor('chat.activity.lifecycle.answering')
  }
  const purpose = STATUS_PURPOSE_CODES[normalized]
  if (purpose) return codeDescriptor(purpose)
  return codeDescriptor('chat.activity.lifecycle.working')
}

/**
 * A step is "semantic" when it names an activity purpose rather than a
 * generic lifecycle phase, and is not the still-live current step. Header
 * counts and the visible step body must share this predicate so they agree
 * by construction.
 */
export function isSemanticActivityStatusStep(step: AssistantActivityStatusStep): boolean {
  return !step.isCurrent && !step.label.code.startsWith('chat.activity.lifecycle.')
}

function projectStatusSteps(
  history: readonly StatusPart[],
  clusters: AssistantActivityCluster[],
  lifecycle: AssistantActivityLifecycle,
): AssistantActivityStatusStep[] {
  const steps: AssistantActivityStatusStep[] = []
  for (const entry of history) {
    const label = statusLabelFor(entry, clusters)
    if (!label) continue
    const previous = steps[steps.length - 1]
    if (previous?.label.code === label.code) continue
    steps.push({
      key: `activity-status:${stableHash(`${entry.action}\u001f${entry.at}`)}`,
      label,
      at: entry.at,
      isCurrent: false,
    })
  }
  if (
    steps.length
    && !clusters.some(cluster => cluster.isCurrent)
    && (lifecycle === 'working' || lifecycle === 'answering')
  ) {
    steps[steps.length - 1].isCurrent = true
  }
  return steps
}

/**
 * Build a deterministic, presentation-neutral activity model from an ordered
 * timeline. Only stable localization codes and numeric parameters are derived;
 * display labels, command text, paths, inputs, and results are never parsed.
 */
export function projectAssistantActivityTimeline(
  timeline: ChatStreamTimelineItem[],
  options: ProjectAssistantActivityOptions = {},
): AssistantActivityTimelineProjection {
  const lifecycle = options.lifecycle ?? 'settled'
  const activityClusters: AssistantActivityCluster[] = []
  let mergeTarget: AssistantActivityCluster | null = null

  for (const item of timeline) {
    if (item.type === 'text') {
      mergeTarget = null
      continue
    }

    for (const call of item.group.calls) {
      const semantic = callSemantic(call)
      const state = callState(call)
      const mergeSemantic = mergeTarget?.calls[0]
        ? callSemantic(mergeTarget.calls[0])
        : null
      const canMerge = state === 'complete'
        && mergeTarget?.state === 'complete'
        && mergeTarget.purpose.code === semantic.purpose
        && mergeSemantic?.footprintKind === semantic.footprintKind

      if (canMerge && mergeTarget) {
        appendCall(mergeTarget, call)
        continue
      }

      const cluster = makeCluster(call, semantic, state, lifecycle)
      activityClusters.push(cluster)
      mergeTarget = state === 'complete' ? cluster : null
    }
  }

  const currentCluster = [...activityClusters].reverse().find(cluster => cluster.isCurrent)
  const statusSteps = projectStatusSteps(
    options.statusHistory ?? [],
    activityClusters,
    lifecycle,
  )
  return {
    lifecycle,
    lifecycleLabel: codeDescriptor(LIFECYCLE_CODES[lifecycle]),
    activityClusters,
    purposeSummary: summarizeCodes(
      activityClusters,
      cluster => cluster.purpose.code,
      cluster => cluster.callCount,
    ),
    footprintSummary: summarizeCodes(
      activityClusters,
      cluster => cluster.footprint.code,
      cluster => descriptorCount(cluster.footprint),
    ),
    currentClusterKey: currentCluster?.key ?? null,
    statusSteps,
  }
}

/**
 * Project a completed assistant message into compact activity and canonical
 * answer surfaces without rewriting the persisted timeline.
 *
 * The terminal `message.text` is the only authoritative answer. A timeline
 * text segment followed by another tool is retained as process narration when
 * it is not already contained in the canonical answer; trailing answer
 * snapshots are excluded. Older rows that lack canonical text keep their
 * original timeline rendering rather than risking hidden content.
 */
export function projectAssistantActivity(
  message: ChatRenderedMessage,
  renderMarkdown: (text: string) => string,
  fallbackToolItems: ChatStreamTimelineItem[] = [],
  options: ProjectAssistantActivityOptions = {},
): AssistantActivityProjection {
  const timeline = message.timelineItems?.length
    ? message.timelineItems
    : fallbackToolItems
  const hasTimelineText = timeline.some(item => item.type === 'text')
  const hasCanonicalAnswer = Boolean(message.text.trim())
  const canSeparateActivity = hasCanonicalAnswer || !hasTimelineText
  const activityItems = canSeparateActivity
    ? hasCanonicalAnswer
      ? separatedActivityItems(timeline, message.text)
      : timeline.slice()
    : []
  const timelineProjection = projectAssistantActivityTimeline(
    activityItems,
    options,
  )

  let toolCount = 0
  let failureCount = 0
  for (const item of activityItems) {
    if (item.type !== 'tool-group') continue
    toolCount += item.group.calls.length
    failureCount += item.group.calls.filter(
      call => call.isError || call.status === 'error',
    ).length
  }

  const answerPart: TextPart | null = canSeparateActivity && hasCanonicalAnswer
    ? {
        type: 'text',
        html: renderMarkdown(message.text),
        rawText: message.text,
        key: `${message.messageId || message.id || 'assistant'}:answer`,
      }
    : null

  return {
    ...timelineProjection,
    canSeparateActivity,
    activityItems,
    answerPart,
    toolCount,
    failureCount,
  }
}
