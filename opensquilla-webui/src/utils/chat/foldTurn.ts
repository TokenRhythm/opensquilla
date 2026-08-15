import type {
  ChatRenderedMessage,
  ChatStreamSegment,
  ChatStreamTimelineItem,
  ChatTimelineSegment,
  ChatToolCall,
  ChatToolCallGroup,
} from '@/types/chat'
import type {
  ChatPart,
  InterruptApprovalData,
  InterruptClarifyData,
  InterruptViewState,
  SourcePart,
  StatusPart,
} from '@/types/parts'
import type { ArtifactPayload } from '@/types/rpc'
import type { Frame, ReasoningBlock } from '@/types/turnlog'
import {
  isEmptyToolPreview,
  toolDisplayName,
  toolOperationKey,
  truncateToolPreview,
} from '@/utils/chat/toolDisplay'
import { segmentsToTimelineItems } from '@/utils/chat/segmentsToTimelineItems'
import { toParts, type ToPartsInterrupt } from '@/utils/chat/toParts'
import { toSources } from '@/utils/chat/toSources'

export interface FoldedTurn {
  // The SAME render surface the legacy live refs expose, rebuilt from frames.
  timelineItems: ChatStreamTimelineItem[]
  toolCalls: ChatToolCall[]
  artifacts: ArtifactPayload[]
  rawText: string
  // Live-only extras (not part of the toParts surface):
  thinkingText: string
  reasoningBlocks: ReasoningBlock[]
  toolTimes: Map<string, { startedAt: number; endedAt?: number }>
  // Derived parts (reuse toParts/toSources, do not reimplement):
  parts: ChatPart[]
  sources: SourcePart[]
  // Accepted activity-phase transitions, in arrival order, for the finished
  // turn's activity timeline. Empty when no status frames were appended.
  statusHistory: StatusPart[]
  timelineSegments: ChatTimelineSegment[]
}

// Live ownerKey: the legacy `streamTimelineItems` computed groups with the
// 'stream' base key, so the fold must use the identical key to match groupIds
// and tool renderKeys exactly (see grouping/ordering identity).
const LIVE_OWNER_KEY = 'stream'

/** One ordered interrupt accumulated by the fold, keyed by approvalId. */
type FoldedInterrupt = ToPartsInterrupt

export interface ReconciledTextSnapshot {
  rawText: string
  segments: ChatStreamSegment[]
  changed: boolean
}

/**
 * Apply an authoritative terminal text snapshot without losing tool history.
 *
 * A strict extension is still an ordinary answer suffix and therefore stays
 * after the last streamed answer segment. A conflicting snapshot supersedes
 * streamed answer text, but keeps tool groups and intermediate commentary in
 * arrival order before the canonical answer. An empty snapshot intentionally
 * clears answer text while retaining that work history.
 */
export function reconcileTextSnapshot(
  segments: ChatStreamSegment[],
  accumulatedText: string,
  snapshot: string,
): ReconciledTextSnapshot {
  if (snapshot === accumulatedText) {
    return { rawText: accumulatedText, segments, changed: false }
  }

  if (accumulatedText && snapshot.startsWith(accumulatedText)) {
    const suffix = snapshot.slice(accumulatedText.length)
    const next = segments.slice()
    const last = next[next.length - 1]
    if (last?.type === 'text' && last.presentation !== 'intermediate') {
      next[next.length - 1] = {
        ...last,
        raw: `${last.raw || ''}${suffix}`,
        dirty: true,
      }
    } else {
      next.push({ type: 'text', raw: suffix, html: '', dirty: true, presentation: 'answer' })
    }
    return { rawText: snapshot, segments: next, changed: true }
  }

  const next = segments.filter(segment => (
    segment.type !== 'text' || segment.presentation === 'intermediate'
  ))
  if (snapshot) {
    next.push({ type: 'text', raw: snapshot, html: '', dirty: true, presentation: 'answer' })
  }
  return { rawText: snapshot, segments: next, changed: true }
}

// A later requested-frame for the same approvalId (a re-broadcast or the
// hydration backfill that carries args/warning) merges richer fields onto the
// already-seen interrupt without reordering — last non-empty write wins per
// field. The two payload shapes never mix for one id (kind is stable), so the
// merge stays within the original sub-kind.
function mergeInterruptData(
  prev: FoldedInterrupt,
  next: InterruptApprovalData | InterruptClarifyData,
): FoldedInterrupt {
  if (prev.kind !== 'approval') return { ...prev, data: { ...prev.data, ...next } }
  const a = prev.data as InterruptApprovalData
  const b = next as InterruptApprovalData
  return {
    ...prev,
    data: {
      ...a,
      ...b,
      // keep already-hydrated args/warning if the newer frame omits them
      args: b.args ?? a.args,
      warning: b.warning || a.warning,
      command: b.command || a.command,
    },
  }
}

function asRenderedMessage(folded: {
  timelineItems: ChatStreamTimelineItem[]
  toolCalls: ChatToolCall[]
  artifacts: ArtifactPayload[]
  rawText: string
}): ChatRenderedMessage {
  return {
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: folded.rawText,
    timeStr: '',
    showHeader: false,
    toolCalls: folded.toolCalls,
    timelineItems: folded.timelineItems,
    artifacts: folded.artifacts,
  }
}

/**
 * Incremental live-turn accumulator.
 *
 * `foldTurn` below intentionally stays a pure replay oracle for history and
 * parity tests.  The live UI, however, must not replay the entire accepted
 * frame log for every token.  This accumulator applies each frame once and
 * only derives the small render projection when the frame scheduler publishes
 * a snapshot.
 */
export class TurnAccumulator {
  private segments: ChatStreamSegment[] = []
  private toolCalls: ChatToolCall[] = []
  private toolCallsById = new Map<string, ChatToolCall>()
  private toolInputChunks = new Map<string, string[]>()
  private toolInputPreview = new Map<string, string>()
  private artifacts: ArtifactPayload[] = []
  private toolTimes = new Map<string, { startedAt: number; endedAt?: number }>()
  private interrupts: FoldedInterrupt[] = []
  private interruptIndex = new Map<string, number>()
  private statusHistory: StatusPart[] = []
  private maintenanceIndex = new Map<string, number>()
  private rawText = ''
  private finalText: string | null = null
  private thinkingText = ''
  private reasoningBlocks: ReasoningBlock[] = []
  private reasoningBlocksById = new Map<string, ReasoningBlock>()
  private toolGroupSeq = 0

  reset(): void {
    this.segments = []
    this.toolCalls = []
    this.toolCallsById = new Map()
    this.toolInputChunks = new Map()
    this.toolInputPreview = new Map()
    this.artifacts = []
    this.toolTimes = new Map()
    this.interrupts = []
    this.interruptIndex = new Map()
    this.statusHistory = []
    this.maintenanceIndex = new Map()
    this.rawText = ''
    this.finalText = null
    this.thinkingText = ''
    this.reasoningBlocks = []
    this.reasoningBlocksById = new Map()
    this.toolGroupSeq = 0
  }

  /** Split a steered turn at its text boundary without replaying a frame log. */
  checkpointText(): void {
    this.segments = this.segments.filter(segment => segment.type !== 'text')
    this.rawText = ''
    this.finalText = null
  }

  /**
   * Read the authoritative current answer without materializing a detached
   * render snapshot. The stream ingress uses this for cumulative-delta
   * de-duplication and terminal commit, keeping the production accumulator as
   * the sole full-text owner between scheduled publications.
   */
  currentRawText(): string {
    return this.finalText ?? this.rawText
  }

  private ensureReasoningBlock(
    blockId: string | undefined,
    blockIndex: number | undefined,
    at: number,
    contentKind: 'summary' | 'reasoning' = 'reasoning',
  ): ReasoningBlock {
    const id = blockId || 'legacy-reasoning'
    const existing = this.reasoningBlocksById.get(id)
    if (existing) return existing

    const active = [...this.reasoningBlocks]
      .reverse()
      .find(block => block.status === 'streaming')
    if (active && active.id !== id) {
      active.status = 'completed'
      active.endedAt = at
    }

    const block: ReasoningBlock = {
      id,
      index: typeof blockIndex === 'number' && blockIndex >= 0
        ? blockIndex
        : this.reasoningBlocks.length,
      text: '',
      status: 'streaming',
      startedAt: at,
      contentKind,
    }
    this.reasoningBlocks.push(block)
    this.reasoningBlocksById.set(id, block)
    return block
  }

  private replaceToolInput(call: ChatToolCall, input: string): void {
    call.inputRaw = input
    call.inputPreview = truncateToolPreview(input, 200)
    call.displayName = toolDisplayName(call.name, input)
    this.toolInputChunks.delete(call.toolId)
    this.toolInputPreview.set(call.toolId, input.slice(0, 200))
  }

  private materializedToolInput(call: ChatToolCall): string {
    const chunks = this.toolInputChunks.get(call.toolId)
    return chunks?.length ? `${call.inputRaw || ''}${chunks.join('')}` : call.inputRaw || ''
  }

  private finalizeToolInput(call: ChatToolCall): boolean {
    const chunks = this.toolInputChunks.get(call.toolId)
    if (!chunks?.length) return false
    this.replaceToolInput(call, this.materializedToolInput(call))
    return true
  }

  /** Materialize pending tool fragments once at the terminal boundary. */
  finalizeToolInputs(): boolean {
    let changed = false
    for (const call of this.toolCalls) changed = this.finalizeToolInput(call) || changed
    return changed
  }

  private ensureCall(
    toolId: string,
    name: string,
    input: string,
    running: boolean,
  ): ChatToolCall {
    const existing = this.toolCallsById.get(toolId)
    if (existing) {
      if (input) this.replaceToolInput(existing, input)
      return existing
    }

    const displacedText = this.segments[this.segments.length - 1]
    if (displacedText?.type === 'text' && displacedText.presentation === 'answer') {
      // A trailing answer is rendered by StreamingTextPart and therefore has
      // no cached HTML. Once a later tool displaces it into the activity
      // chronology, mark it dirty so the next published snapshot renders it.
      displacedText.dirty = true
    }
    const operationKey = toolOperationKey(name)
    const lastSegment = this.segments[this.segments.length - 1]
    const groupId = lastSegment?.type === 'tool-group'
      && lastSegment.operationKey === operationKey
      && lastSegment.groupId
      ? lastSegment.groupId
      : `stream:tool-group:${operationKey}:${this.toolGroupSeq++}`

    if (lastSegment?.type !== 'tool-group' || lastSegment.groupId !== groupId) {
      this.segments.push({ type: 'tool-group', groupId, operationKey })
    }

    const call: ChatToolCall = {
      toolId,
      name,
      displayName: toolDisplayName(name, input),
      groupId,
      inputRaw: input,
      inputPreview: truncateToolPreview(input, 200),
      isRunning: running,
      status: '',
      isError: false,
      result: '',
      resultPreview: '',
      isOpen: false,
    }
    this.toolCalls.push(call)
    this.toolCallsById.set(toolId, call)
    this.toolInputPreview.set(toolId, input.slice(0, 200))
    return call
  }

  append(frame: Frame): void {
    switch (frame.kind) {
      case 'text': {
        this.rawText += frame.text
        const lastSegment = this.segments[this.segments.length - 1]
        if (
          !lastSegment
          || lastSegment.type !== 'text'
          || lastSegment.presentation !== frame.presentation
        ) {
          this.segments.push({
            type: 'text',
            raw: frame.text,
            html: '',
            dirty: true,
            presentation: frame.presentation,
          })
        } else {
          lastSegment.raw = `${lastSegment.raw || ''}${frame.text}`
          lastSegment.dirty = true
        }
        break
      }
      case 'tool-start': {
        if (!this.toolTimes.has(frame.toolId)) {
          this.toolTimes.set(frame.toolId, { startedAt: frame.at })
        }
        this.ensureCall(frame.toolId, frame.name, frame.input, true)
        break
      }
      case 'tool-delta': {
        const call = this.toolCallsById.get(frame.toolId)
        if (!call) break
        const chunks = this.toolInputChunks.get(frame.toolId)
        if (chunks) chunks.push(frame.fragment)
        else this.toolInputChunks.set(frame.toolId, [frame.fragment])
        const previousPreview = this.toolInputPreview.get(frame.toolId) || ''
        if (previousPreview.length < 200) {
          const nextPreview = `${previousPreview}${frame.fragment}`.slice(0, 200)
          this.toolInputPreview.set(frame.toolId, nextPreview)
          if (!isEmptyToolPreview(nextPreview)) {
            call.inputPreview = truncateToolPreview(nextPreview, 200)
            call.displayName = toolDisplayName(call.name, nextPreview)
          }
        }
        break
      }
      case 'tool-result': {
        const call = this.ensureCall(frame.toolId, frame.name, frame.input, false)
        if (!frame.input) this.finalizeToolInput(call)
        call.isRunning = false
        call.status = frame.isError ? 'error' : 'success'
        call.isError = frame.isError
        call.result = frame.result
        call.resultPreview = truncateToolPreview(frame.result, 200)
        const timing = this.toolTimes.get(call.toolId)
        if (timing && !timing.endedAt) timing.endedAt = frame.at
        break
      }
      case 'artifact':
        this.artifacts.push(frame.artifact)
        break
      case 'thinking-start':
        this.ensureReasoningBlock(
          frame.blockId,
          frame.blockIndex,
          frame.at,
          frame.contentKind,
        )
        break
      case 'thinking': {
        this.thinkingText += frame.text
        const block = this.ensureReasoningBlock(
          frame.blockId,
          frame.blockIndex,
          frame.at,
        )
        block.text += frame.text
        break
      }
      case 'thinking-end': {
        const block = this.ensureReasoningBlock(
          frame.blockId,
          frame.blockIndex,
          frame.at,
        )
        block.status = frame.status
        block.endedAt = frame.at
        break
      }
      case 'final-text':
        this.finalText = frame.text
        break
      case 'interrupt': {
        const index = this.interruptIndex.get(frame.approvalId)
        if (index === undefined) {
          this.interruptIndex.set(frame.approvalId, this.interrupts.length)
          this.interrupts.push({
            kind: frame.interruptKind,
            approvalId: frame.approvalId,
            data: frame.data,
          })
          const displacedText = this.segments[this.segments.length - 1]
          if (displacedText?.type === 'text' && displacedText.presentation === 'answer') {
            displacedText.dirty = true
          }
          this.segments.push({ type: 'interrupt', approvalId: frame.approvalId })
        } else {
          this.interrupts[index] = mergeInterruptData(
            this.interrupts[index]!,
            frame.data,
          )
        }
        break
      }
      case 'interrupt-resolved':
        break
      case 'status': {
        const entry: StatusPart = {
          action: frame.action,
          label: frame.label,
          at: frame.at,
          ...(frame.id ? { id: frame.id } : {}),
          ...(frame.category ? { category: frame.category } : {}),
          ...(frame.state ? { state: frame.state } : {}),
          ...(frame.source ? { source: frame.source } : {}),
          ...(frame.durability ? { durability: frame.durability } : {}),
          ...(frame.detail ? { detail: frame.detail } : {}),
          ...(frame.reason ? { reason: frame.reason } : {}),
        }
        if (entry.category === 'maintenance' && entry.id) {
          const index = this.maintenanceIndex.get(entry.id)
          if (index === undefined) {
            this.maintenanceIndex.set(entry.id, this.statusHistory.length)
            this.statusHistory.push(entry)
          } else {
            this.statusHistory[index] = {
              ...this.statusHistory[index],
              ...entry,
              at: this.statusHistory[index]!.at,
            }
          }
        } else {
          this.statusHistory.push(entry)
        }
        break
      }
    }
  }

  snapshot(
    renderMarkdown: (text: string) => string,
    toolCallGroups: (
      calls: ChatToolCall[] | undefined,
      baseKey: string,
    ) => ChatToolCallGroup[],
    ownerKey: string = LIVE_OWNER_KEY,
    interruptState: ReadonlyMap<string, InterruptViewState> = new Map(),
    renderAnswerMarkdown: boolean = true,
    materializeRunningToolInputs: boolean = true,
  ): FoldedTurn {
    if (this.finalText !== null) {
      const reconciled = reconcileTextSnapshot(
        this.segments,
        this.rawText,
        this.finalText,
      )
      this.rawText = reconciled.rawText
      if (reconciled.segments !== this.segments) {
        this.segments.splice(0, this.segments.length, ...reconciled.segments)
      }
    }

    const trailingSegment = this.segments[this.segments.length - 1]
    for (const segment of this.segments) {
      if (segment.type === 'text' && segment.dirty) {
        // The production live answer is rendered by StreamingTextPart from
        // canonical raw text. Parsing the same growing answer here produced an
        // invisible full-prefix Markdown pass on every visual flush. Keep the
        // rendered HTML only for intermediate narration and for the DEV shadow
        // renderer, which still needs an exact legacy parity surface.
        // Only the current trailing answer is owned by StreamingTextPart. If
        // a later tool arrives, that provisional answer moves back into the
        // activity chronology and needs rendered HTML like any other
        // narration segment.
        segment.html = segment === trailingSegment
          && segment.presentation === 'answer'
          && !renderAnswerMarkdown
          ? ''
          : renderMarkdown(segment.raw || '')
        segment.dirty = false
      }
    }

    // Publish detached collections. The accumulator continues mutating its
    // private state between frames; already-published snapshots must stay
    // immutable until the scheduler explicitly publishes the next one.
    const segments = this.segments.map(segment => ({ ...segment }))
    const toolCalls = this.toolCalls.map(call => ({
      ...call,
      ...(materializeRunningToolInputs
        ? { inputRaw: this.materializedToolInput(call) }
        : {}),
    }))
    const artifacts = this.artifacts.map(artifact => ({ ...artifact }))
    const interrupts = this.interrupts.map(interrupt => ({
      ...interrupt,
      data: { ...interrupt.data },
    }))
    const statusHistory = this.statusHistory.map(entry => ({ ...entry }))
    const reasoningBlocks = this.reasoningBlocks.map(block => ({ ...block }))

    const interruptParts = new Map<
      string,
      Extract<ChatPart, { type: 'interrupt' }>
    >()
    for (const interrupt of interrupts) {
      const state = interruptState.get(interrupt.approvalId)
      interruptParts.set(interrupt.approvalId, {
        type: 'interrupt',
        interruptKind: interrupt.kind,
        approval: interrupt.kind === 'approval'
          ? interrupt.data as InterruptApprovalData
          : undefined,
        clarify: interrupt.kind === 'clarify'
          ? interrupt.data as InterruptClarifyData
          : undefined,
        resolution: state?.resolution ?? interrupt.resolution ?? null,
        busy: state?.busy ?? false,
        error: state?.error ?? '',
        key: `${ownerKey}:interrupt:${interrupt.approvalId}`,
      })
    }

    const timelineItems = segmentsToTimelineItems(
      segments,
      toolCalls,
      ownerKey,
      interruptParts,
    )
    const timelineSegments = segments.flatMap((segment): ChatTimelineSegment[] => {
      if (segment.type === 'text') {
        const raw = String(segment.raw || '')
        return raw ? [{ type: 'text', raw, presentation: segment.presentation }] : []
      }
      if (segment.type === 'interrupt') {
        const approvalId = String(segment.approvalId || '')
        return approvalId ? [{ type: 'interrupt', approvalId }] : []
      }
      return [{
        type: 'tool-group',
        groupId: segment.groupId,
        operationKey: segment.operationKey,
      }]
    })
    const base = {
      timelineItems,
      toolCalls,
      artifacts,
      rawText: this.rawText,
    }
    const rendered = asRenderedMessage(base)
    return {
      ...base,
      thinkingText: this.thinkingText,
      reasoningBlocks,
      toolTimes: new Map(
        [...this.toolTimes].map(([key, value]) => [key, { ...value }]),
      ),
      statusHistory,
      timelineSegments,
      parts: toParts(
        rendered,
        renderMarkdown,
        toolCallGroups,
        ownerKey,
        interrupts,
        interruptState,
      ),
      sources: toSources(rendered),
    }
  }
}

/**
 * Pure left-fold of an append-only frame log into the exact structures the
 * legacy live mutators build in place (text segments, tool calls, artifacts,
 * raw text, tool clocks, live thinking). The frame array is already in accept
 * order, so the fold simply replays it; grouping and ordering are rebuilt with
 * the same rules `ensureStreamToolCall` uses so groupIds/renderKeys match.
 */
export function foldTurn(
  events: Frame[],
  renderMarkdown: (text: string) => string,
  toolCallGroups: (calls: ChatToolCall[] | undefined, baseKey: string) => ChatToolCallGroup[],
  ownerKey: string = LIVE_OWNER_KEY,
  interruptState: ReadonlyMap<string, InterruptViewState> = new Map(),
): FoldedTurn {
  const segments: ChatStreamSegment[] = []
  const toolCalls: ChatToolCall[] = []
  const toolCallsById = new Map<string, ChatToolCall>()
  const artifacts: ArtifactPayload[] = []
  const toolTimes = new Map<string, { startedAt: number; endedAt?: number }>()
  // Ordered interrupts, deduped/merged by approvalId in arrival order.
  const interrupts: FoldedInterrupt[] = []
  const interruptIndex = new Map<string, number>()
  const statusHistory: StatusPart[] = []
  const maintenanceIndex = new Map<string, number>()
  let rawText = ''
  let finalText: string | null = null
  let thinkingText = ''
  const reasoningBlocks: ReasoningBlock[] = []
  const reasoningBlocksById = new Map<string, ReasoningBlock>()
  let toolGroupSeq = 0

  function ensureReasoningBlock(
    blockId: string | undefined,
    blockIndex: number | undefined,
    at: number,
    contentKind: 'summary' | 'reasoning' = 'reasoning',
  ): ReasoningBlock {
    const id = blockId || 'legacy-reasoning'
    const existing = reasoningBlocksById.get(id)
    if (existing) return existing

    // A new explicit block is an authoritative boundary. If an older producer
    // omitted its matching end event, settle only the previously active block
    // instead of merging both streams together.
    let active: ReasoningBlock | undefined
    for (let index = reasoningBlocks.length - 1; index >= 0; index -= 1) {
      if (reasoningBlocks[index]?.status === 'streaming') {
        active = reasoningBlocks[index]
        break
      }
    }
    if (active && active.id !== id) {
      active.status = 'completed'
      active.endedAt = at
    }
    const block: ReasoningBlock = {
      id,
      index: typeof blockIndex === 'number' && blockIndex >= 0
        ? blockIndex
        : reasoningBlocks.length,
      text: '',
      status: 'streaming',
      startedAt: at,
      contentKind,
    }
    reasoningBlocks.push(block)
    reasoningBlocksById.set(id, block)
    return block
  }

  // Mirror ensureStreamToolCall's group derivation: a tool joins the trailing
  // tool-group segment when the operationKey matches, else opens a new group
  // with a monotonic counter. Returns the existing call when already seen.
  function ensureCall(toolId: string, name: string, input: string, running: boolean): ChatToolCall {
    const existing = toolCallsById.get(toolId)
    if (existing) {
      if (input) {
        existing.inputRaw = input
        existing.inputPreview = truncateToolPreview(input, 200)
        existing.displayName = toolDisplayName(existing.name, input)
      }
      return existing
    }

    const operationKey = toolOperationKey(name)
    const lastSegment = segments[segments.length - 1]
    const groupId = lastSegment?.type === 'tool-group' && lastSegment.operationKey === operationKey && lastSegment.groupId
      ? lastSegment.groupId
      : `stream:tool-group:${operationKey}:${toolGroupSeq++}`

    if (lastSegment?.type !== 'tool-group' || lastSegment.groupId !== groupId) {
      segments.push({ type: 'tool-group', groupId, operationKey })
    }

    const call: ChatToolCall = {
      toolId,
      name,
      displayName: toolDisplayName(name, input),
      groupId,
      inputRaw: input,
      inputPreview: truncateToolPreview(input, 200),
      isRunning: running,
      status: '',
      isError: false,
      result: '',
      resultPreview: '',
      isOpen: false,
    }
    toolCalls.push(call)
    toolCallsById.set(toolId, call)
    return call
  }

  for (const frame of events) {
    switch (frame.kind) {
      case 'text': {
        rawText += frame.text
        const lastSegment = segments[segments.length - 1]
        if (
          !lastSegment
          || lastSegment.type !== 'text'
          || lastSegment.presentation !== frame.presentation
        ) {
          segments.push({
            type: 'text',
            raw: frame.text,
            html: '',
            dirty: true,
            presentation: frame.presentation,
          })
        } else {
          lastSegment.raw = (lastSegment.raw || '') + frame.text
          lastSegment.dirty = true
        }
        break
      }
      case 'tool-start': {
        // result-only calls get no clock; only start frames stamp startedAt
        if (!toolTimes.has(frame.toolId)) {
          toolTimes.set(frame.toolId, { startedAt: frame.at })
        }
        ensureCall(frame.toolId, frame.name, frame.input, true)
        break
      }
      case 'tool-delta': {
        const tc = toolCallsById.get(frame.toolId)
        if (!tc) break
        const nextInput = `${tc.inputRaw || ''}${frame.fragment}`
        tc.inputRaw = nextInput
        if (!isEmptyToolPreview(nextInput)) {
          tc.inputPreview = truncateToolPreview(nextInput, 200)
          tc.displayName = toolDisplayName(tc.name, nextInput)
        }
        break
      }
      case 'tool-result': {
        const tc = ensureCall(frame.toolId, frame.name, frame.input, false)
        tc.isRunning = false
        tc.status = frame.isError ? 'error' : 'success'
        tc.isError = frame.isError
        tc.result = frame.result
        tc.resultPreview = truncateToolPreview(frame.result, 200)
        const timing = toolTimes.get(tc.toolId)
        if (timing && !timing.endedAt) timing.endedAt = frame.at
        break
      }
      case 'artifact': {
        artifacts.push(frame.artifact)
        break
      }
      case 'thinking-start': {
        ensureReasoningBlock(
          frame.blockId,
          frame.blockIndex,
          frame.at,
          frame.contentKind,
        )
        break
      }
      case 'thinking': {
        thinkingText += frame.text
        const block = ensureReasoningBlock(
          frame.blockId,
          frame.blockIndex,
          frame.at,
        )
        block.text += frame.text
        break
      }
      case 'thinking-end': {
        const block = ensureReasoningBlock(
          frame.blockId,
          frame.blockIndex,
          frame.at,
        )
        block.status = frame.status
        block.endedAt = frame.at
        break
      }
      case 'final-text': {
        // Empty is meaningful: it clears stale streamed text while retaining
        // any tool groups. A missing snapshot emits no frame at all.
        finalText = frame.text
        break
      }
      case 'interrupt': {
        const i = interruptIndex.get(frame.approvalId)
        if (i === undefined) {
          interruptIndex.set(frame.approvalId, interrupts.length)
          interrupts.push({ kind: frame.interruptKind, approvalId: frame.approvalId, data: frame.data })
          segments.push({ type: 'interrupt', approvalId: frame.approvalId })
        } else {
          // A later requested-frame for the same id (re-broadcast / hydration
          // backfill) merges richer data without reordering.
          interrupts[i] = mergeInterruptData(interrupts[i], frame.data)
        }
        break
      }
      case 'interrupt-resolved': {
        // Resolution is read from interruptState (the optimistic, idempotent
        // side-map); the frame is the forward wire format only. Ignored here.
        break
      }
      case 'status': {
        const entry: StatusPart = {
          action: frame.action,
          label: frame.label,
          at: frame.at,
          ...(frame.id ? { id: frame.id } : {}),
          ...(frame.category ? { category: frame.category } : {}),
          ...(frame.state ? { state: frame.state } : {}),
          ...(frame.source ? { source: frame.source } : {}),
          ...(frame.durability ? { durability: frame.durability } : {}),
          ...(frame.detail ? { detail: frame.detail } : {}),
          ...(frame.reason ? { reason: frame.reason } : {}),
        }
        // Context maintenance emits started/observed/completed lifecycle
        // frames. Keep its first chronological position and update that row in
        // place so one compaction never looks like multiple task steps.
        if (entry.category === 'maintenance' && entry.id) {
          const index = maintenanceIndex.get(entry.id)
          if (index === undefined) {
            maintenanceIndex.set(entry.id, statusHistory.length)
            statusHistory.push(entry)
          } else {
            statusHistory[index] = {
              ...statusHistory[index],
              ...entry,
              at: statusHistory[index]!.at,
            }
          }
        } else {
          // Phase rows remain append-only and in accepted order.
          statusHistory.push(entry)
        }
        break
      }
    }
  }

  if (finalText !== null) {
    const reconciled = reconcileTextSnapshot(segments, rawText, finalText)
    rawText = reconciled.rawText
    if (reconciled.segments !== segments) segments.splice(0, segments.length, ...reconciled.segments)
  }

  // Render text segment html with the same renderer the legacy flush uses, so
  // compared html matches after a flush (synchronous here; ).
  for (const seg of segments) {
    if (seg.type === 'text' && seg.dirty) {
      seg.html = renderMarkdown(seg.raw || '')
      seg.dirty = false
    }
  }

  const interruptParts = new Map<
    string,
    Extract<ChatPart, { type: 'interrupt' }>
  >()
  for (const interrupt of interrupts) {
    const state = interruptState.get(interrupt.approvalId)
    interruptParts.set(interrupt.approvalId, {
      type: 'interrupt',
      interruptKind: interrupt.kind,
      approval: interrupt.kind === 'approval'
        ? interrupt.data as InterruptApprovalData
        : undefined,
      clarify: interrupt.kind === 'clarify'
        ? interrupt.data as InterruptClarifyData
        : undefined,
      resolution: state?.resolution ?? interrupt.resolution ?? null,
      busy: state?.busy ?? false,
      error: state?.error ?? '',
      key: `${ownerKey}:interrupt:${interrupt.approvalId}`,
    })
  }
  const timelineItems = segmentsToTimelineItems(
    segments,
    toolCalls,
    ownerKey,
    interruptParts,
  )
  const timelineSegments = segments.flatMap((segment): ChatTimelineSegment[] => {
    if (segment.type === 'text') {
      const raw = String(segment.raw || '')
      return raw ? [{ type: 'text', raw, presentation: segment.presentation }] : []
    }
    if (segment.type === 'interrupt') {
      const approvalId = String(segment.approvalId || '')
      return approvalId ? [{ type: 'interrupt', approvalId }] : []
    }
    return [{
      type: 'tool-group',
      groupId: segment.groupId,
      operationKey: segment.operationKey,
    }]
  })
  const base = { timelineItems, toolCalls, artifacts, rawText }
  const rendered = asRenderedMessage(base)

  return {
    ...base,
    thinkingText,
    reasoningBlocks,
    toolTimes,
    statusHistory,
    timelineSegments,
    parts: toParts(rendered, renderMarkdown, toolCallGroups, ownerKey, interrupts, interruptState),
    sources: toSources(rendered),
  }
}
