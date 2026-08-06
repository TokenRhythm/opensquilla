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
  | 'chat.compact.compacting'
  | 'chat.compact.compacted'
  | 'chat.compact.withinBudget'
  | 'chat.compact.cancelled'
  | 'chat.compact.failed'

export interface AssistantActivityStatusStep {
  key: string
  label: AssistantActivityCodeDescriptor<AssistantActivityStatusCode>
  at: number
  isCurrent: boolean
  id?: string
  category?: 'phase' | 'maintenance'
  state?: 'running' | 'completed' | 'skipped' | 'stale' | 'cancelled' | 'failed'
  source?: string
  durability?: string
  detail?: string
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

export interface LiveAssistantTimelineSplitOptions {
  /**
   * Keep unclassified/intermediate text inside the activity transcript once
   * the turn has used a tool. A gateway-confirmed `answer` span may still
   * stream outside without relying on a timing heuristic.
   */
  keepToolTurnTextInActivity?: boolean
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
  answerSource: AssistantAnswerSource
  toolCount: number
  failureCount: number
}

export type AssistantAnswerSource =
  | 'canonical'
  | 'terminal-timeline-boundary'
  | 'terminal-control-boundary'
  | 'none'

export interface AssistantAnswerResolution {
  text: string
  source: AssistantAnswerSource
  activityItems: ChatStreamTimelineItem[]
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
// These tools persist execution-control state that already has a dedicated
// Plan/Goal surface. A successful call is not user work and must not inflate
// the generic tool count. It is also answer-transparent: a terminal summary
// can immediately precede the control call because that call ends the turn.
//
// `update_plan` remains only as a history-compatibility spelling. The runtime
// no longer registers it.
const ANSWER_TRANSPARENT_CONTROL_TOOLS = new Set([
  'plan_run_checkpoint',
  'update_plan',
])
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
 * Treat only the current trailing text segment as an answer candidate. After
 * a tool has run, the gateway's presentation marker must confirm that segment
 * as an answer; unclassified/intermediate text remains chronological activity.
 */
export function splitLiveAssistantTimeline(
  timeline: ChatStreamTimelineItem[],
  options: LiveAssistantTimelineSplitOptions = {},
): LiveAssistantTimelineSplit {
  const last = timeline[timeline.length - 1]
  if (
    options.keepToolTurnTextInActivity
    && timeline.some(item => item.type === 'tool-group')
    && (
      last?.type !== 'text'
      || last.presentation !== 'answer'
    )
  ) {
    return { activityItems: timeline.slice(), answerItem: null }
  }

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
    if (item.type === 'tool-group' || item.type === 'interrupt') return true
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

function isSuccessfulAnswerTransparentControlGroup(
  item: ChatStreamTimelineItem,
): item is Extract<ChatStreamTimelineItem, { type: 'tool-group' }> {
  return item.type === 'tool-group'
    && item.group.calls.length > 0
    && item.group.calls.every(call =>
      ANSWER_TRANSPARENT_CONTROL_TOOLS.has(activityToolName(call.name))
      && !call.isRunning
      && !call.isError
      && call.status === 'success',
    )
}

function normalizedComparableText(value: string): string {
  // Preserve Markdown-significant interior whitespace. Only normalize the
  // transport-level line-ending difference and outer padding.
  return String(value || '').replace(/\r\n?/g, '\n').trim()
}

function timelineTextAggregates(
  timeline: ChatStreamTimelineItem[],
): string[] | null {
  const textItems = timeline.filter(
    (item): item is Extract<ChatStreamTimelineItem, { type: 'text' }> =>
      item.type === 'text',
  )
  if (!textItems.length) return null

  const chunks: string[] = []
  for (const item of textItems) {
    // HTML cannot be losslessly compared with raw Markdown. Missing rawText is
    // therefore an unknown boundary, not permission to hide content.
    if (typeof item.rawText !== 'string') return null
    chunks.push(item.rawText)
  }
  const compact = chunks.join('')
  if (chunks.length < 2) return [compact]

  // Mirrors the gateway's persisted `_readable_tool_boundary_text`: separate
  // narration segments receive a paragraph break only when neither adjacent
  // segment already supplies whitespace.
  let readable = chunks[0] || ''
  for (const chunk of chunks.slice(1)) {
    readable += readable.slice(-1).trim() && chunk.slice(0, 1).trim()
      ? `\n\n${chunk}`
      : chunk
  }
  return readable === compact ? [compact] : [compact, readable]
}

interface TerminalAnswerCandidate {
  text: string
  indexes: Set<number>
  activityPrefix?: Extract<ChatStreamTimelineItem, { type: 'text' }>
}

function splitTerminalMarkdownAnswer(
  text: string,
): { activityText: string, answerText: string } | null {
  const normalized = text.replace(/\r\n?/g, '\n')
  const thematicBreak = /^[ \t]{0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})[ \t]*$/gm

  for (const match of normalized.matchAll(thematicBreak)) {
    const index = match.index
    if (index === undefined) continue
    const activityText = normalized.slice(0, index).trim()
    const answerText = normalized.slice(index + match[0].length).trim()
    if (activityText && answerText) return { activityText, answerText }
  }
  return null
}

/**
 * Recover the terminal answer from an ordinary tool transcript whose
 * persisted message.text is the concatenation of every narration fragment.
 *
 * A non-empty trailing text run after an ordinary tool group is the same
 * structural answer boundary the live renderer already uses. Requiring the
 * canonical text to exactly equal either the compact timeline aggregate or
 * the gateway's readable persisted form keeps this fail-open: when the
 * persisted payload is incomplete or disagrees, no text is hidden.
 */
function terminalTimelineAnswerCandidate(
  timeline: ChatStreamTimelineItem[],
): TerminalAnswerCandidate | null {
  let ordinaryToolGroups = 0
  let lastOrdinaryToolIndex = -1
  for (let index = 0; index < timeline.length; index += 1) {
    const item = timeline[index]
    if (item?.type !== 'tool-group' || isSuccessfulAnswerTransparentControlGroup(item)) {
      continue
    }
    ordinaryToolGroups += 1
    lastOrdinaryToolIndex = index
  }
  if (ordinaryToolGroups < 1 || lastOrdinaryToolIndex < 0) return null

  const indexes = new Set<number>()
  const chunks: string[] = []
  for (let index = lastOrdinaryToolIndex + 1; index < timeline.length; index += 1) {
    const item = timeline[index]
    if (!item) continue
    if (item.type === 'tool-group') {
      if (isSuccessfulAnswerTransparentControlGroup(item)) continue
      return null
    }
    if (item.type !== 'text' || typeof item.rawText !== 'string') return null
    indexes.add(index)
    chunks.push(item.rawText)
  }
  const text = chunks.join('')
  if (!text.trim()) return null

  // Some providers finish the last tool call with a short transition and the
  // final answer in the same text item, separated by a Markdown thematic
  // break. Preserve that transition in the activity chronology and expose
  // only the content after the explicit boundary as the answer.
  const markdownSplit = splitTerminalMarkdownAnswer(text)
  if (markdownSplit) {
    const firstIndex = Math.min(...indexes)
    const firstItem = timeline[firstIndex]
    return {
      text: markdownSplit.answerText,
      indexes,
      activityPrefix: {
        type: 'text',
        key: `${firstItem?.key || 'terminal'}:activity-prefix`,
        rawText: markdownSplit.activityText,
        html: '',
      },
    }
  }
  return { text, indexes }
}

function terminalControlAnswerCandidate(
  timeline: ChatStreamTimelineItem[],
): TerminalAnswerCandidate | null {
  let index = timeline.length - 1
  let crossedControlBoundary = false

  while (index >= 0) {
    const item = timeline[index]
    if (!item) {
      index -= 1
      continue
    }
    if (item.type === 'tool-group') {
      if (!isSuccessfulAnswerTransparentControlGroup(item)) return null
      crossedControlBoundary = true
      index -= 1
      continue
    }
    if (item.type !== 'text') return null
    if (!String(item.rawText || '').trim() && !String(item.html || '').trim()) {
      index -= 1
      continue
    }
    break
  }

  if (!crossedControlBoundary || index < 0 || timeline[index]?.type !== 'text') {
    return null
  }

  const indexes = new Set<number>()
  const chunks: string[] = []
  while (index >= 0) {
    const item = timeline[index]
    if (!item || item.type !== 'text') break
    if (typeof item.rawText !== 'string') return null
    indexes.add(index)
    chunks.unshift(item.rawText)
    index -= 1
  }
  const text = chunks.join('')
  return text.trim() ? { text, indexes } : null
}

function completedAnswerLifecycle(
  message: ChatRenderedMessage,
  lifecycle: AssistantActivityLifecycle,
): boolean {
  return lifecycle === 'settled'
    && !message.isStreaming
    && !message.interrupted
    && !message.terminalFailure
}

/**
 * Resolve the user-facing answer without parsing model prose.
 *
 * Newer runtimes should eventually persist an explicit answer phase. For old
 * PlanRun rows, `message.text` is the concatenation of every narration segment.
 * We may recover the terminal answer only when all of these structural facts
 * agree: the turn settled successfully, the canonical text exactly matches a
 * supported persisted timeline aggregate, and the last text run is followed
 * only by successful answer-transparent control calls. Every uncertain case
 * fails open to the canonical text.
 */
export function resolveAssistantAnswer(
  message: ChatRenderedMessage,
  timeline: ChatStreamTimelineItem[] = message.timelineItems ?? [],
  lifecycle: AssistantActivityLifecycle = 'settled',
): AssistantAnswerResolution {
  const visibleTimeline = timeline.filter(
    item => !isSuccessfulAnswerTransparentControlGroup(item),
  )
  const canonical = String(message.text || '')
  const aggregates = timelineTextAggregates(timeline)
  const completed = completedAnswerLifecycle(message, lifecycle)
  const aggregateMatchesCanonical = aggregates !== null
    && aggregates.some(aggregate =>
      normalizedComparableText(canonical) === normalizedComparableText(aggregate),
    )
  const controlCandidate = terminalControlAnswerCandidate(timeline)
  const canUseControlBoundary = completed
    && aggregateMatchesCanonical
    && controlCandidate !== null
  if (canUseControlBoundary && controlCandidate) {
    return {
      text: controlCandidate.text,
      source: 'terminal-control-boundary',
      activityItems: timeline.filter(
        (item, index) =>
          !controlCandidate.indexes.has(index)
          && !isSuccessfulAnswerTransparentControlGroup(item),
      ),
    }
  }

  const timelineCandidate = terminalTimelineAnswerCandidate(timeline)
  const canUseTimelineBoundary = completed
    && aggregateMatchesCanonical
    && timelineCandidate !== null
  if (canUseTimelineBoundary && timelineCandidate) {
    const activityItems = timeline.filter(
      (item, index) =>
        !timelineCandidate.indexes.has(index)
        && !isSuccessfulAnswerTransparentControlGroup(item),
    )
    if (timelineCandidate.activityPrefix) {
      activityItems.push(timelineCandidate.activityPrefix)
    }
    return {
      text: timelineCandidate.text,
      source: 'terminal-timeline-boundary',
      activityItems,
    }
  }

  return {
    text: canonical,
    source: canonical.trim() ? 'canonical' : 'none',
    activityItems: visibleTimeline,
  }
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
  if (entry.category === 'maintenance') {
    if (entry.state === 'failed') return codeDescriptor('chat.compact.failed')
    if (entry.state === 'skipped') return codeDescriptor('chat.compact.withinBudget')
    if (entry.state === 'stale' || entry.state === 'cancelled') {
      return codeDescriptor('chat.compact.cancelled')
    }
    if (entry.state === 'running') return codeDescriptor('chat.compact.compacting')
    return codeDescriptor('chat.compact.compacted')
  }
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
  return step.category !== 'maintenance'
    && !step.isCurrent
    && !step.label.code.startsWith('chat.activity.lifecycle.')
}

function isAutomaticCompletedMaintenance(step: AssistantActivityStatusStep): boolean {
  return step.category === 'maintenance'
    && step.state === 'completed'
    && String(step.source || '').toLowerCase() === 'automatic'
    && Boolean(step.id)
}

/**
 * One automatic compaction may be observed through both a transient request
 * lifecycle and the durable history rewrite. When the backend uses different
 * ids for those adjacent terminal observations, present them as one maintenance
 * result. A failure or any intervening phase is a hard boundary.
 */
function mergeAdjacentAutomaticCompletedMaintenance(
  steps: AssistantActivityStatusStep[],
): AssistantActivityStatusStep[] {
  const merged: AssistantActivityStatusStep[] = []
  for (const step of steps) {
    const previous = merged[merged.length - 1]
    if (
      previous
      && isAutomaticCompletedMaintenance(previous)
      && isAutomaticCompletedMaintenance(step)
      && previous.id !== step.id
    ) {
      const preferred = step.durability === 'durable' && previous.durability !== 'durable'
        ? step
        : previous
      merged[merged.length - 1] = {
        ...preferred,
        // Preserve the first visual position and keyed DOM row while allowing
        // durable metadata (including its id) to become authoritative.
        key: previous.key,
        at: previous.at,
      }
      continue
    }
    merged.push(step)
  }
  return merged
}

function projectStatusSteps(
  history: readonly StatusPart[],
  clusters: AssistantActivityCluster[],
  lifecycle: AssistantActivityLifecycle,
): AssistantActivityStatusStep[] {
  const steps: AssistantActivityStatusStep[] = []
  const maintenanceById = new Map<string, number>()
  for (const entry of history) {
    const label = statusLabelFor(entry, clusters)
    if (!label) continue
    if (entry.category === 'maintenance') {
      const step: AssistantActivityStatusStep = {
        key: `activity-maintenance:${entry.id || stableHash(`${entry.at}`)}`,
        label,
        at: entry.at,
        isCurrent: entry.state === 'running',
        id: entry.id,
        category: 'maintenance',
        state: entry.state,
        source: entry.source,
        durability: entry.durability,
        detail: entry.detail,
      }
      if (entry.id && maintenanceById.has(entry.id)) {
        steps[maintenanceById.get(entry.id)!] = step
      } else {
        if (entry.id) maintenanceById.set(entry.id, steps.length)
        steps.push(step)
      }
      continue
    }
    const previous = steps[steps.length - 1]
    if (previous?.label.code === label.code) continue
    steps.push({
      key: `activity-status:${stableHash(`${entry.action}\u001f${entry.at}`)}`,
      label,
      at: entry.at,
      isCurrent: false,
      category: 'phase',
    })
  }
  const mergedSteps = mergeAdjacentAutomaticCompletedMaintenance(steps)
  if (
    mergedSteps.length
    && !clusters.some(cluster => cluster.isCurrent)
    && (lifecycle === 'working' || lifecycle === 'answering')
  ) {
    const lastPhase = [...mergedSteps].reverse().find(step => step.category !== 'maintenance')
    if (lastPhase) lastPhase.isCurrent = true
  }
  return mergedSteps
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
    if (item.type !== 'tool-group') {
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
  const lifecycle = options.lifecycle ?? 'settled'
  const answerResolution = resolveAssistantAnswer(message, timeline, lifecycle)
  const hasTimelineText = timeline.some(item => item.type === 'text')
  const hasCanonicalAnswer = Boolean(answerResolution.text.trim())
  const canSeparateActivity = hasCanonicalAnswer || !hasTimelineText
  const hasStructuralAnswerBoundary =
    answerResolution.source === 'terminal-control-boundary'
    || answerResolution.source === 'terminal-timeline-boundary'
  const rawActivityItems = canSeparateActivity
    ? hasCanonicalAnswer
      ? hasStructuralAnswerBoundary
        ? answerResolution.activityItems
        : separatedActivityItems(
            answerResolution.activityItems,
            answerResolution.text,
          )
      : answerResolution.activityItems
    : []
  const activityItems = rawActivityItems.map(item =>
    item.type === 'text' && !item.html && item.rawText
      ? { ...item, html: renderMarkdown(item.rawText) }
      : item,
  )
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
        html: renderMarkdown(answerResolution.text),
        rawText: answerResolution.text,
        key: `${message.messageId || message.id || 'assistant'}:answer`,
      }
    : null

  return {
    ...timelineProjection,
    canSeparateActivity,
    activityItems,
    answerPart,
    answerSource: answerResolution.source,
    toolCount,
    failureCount,
  }
}
