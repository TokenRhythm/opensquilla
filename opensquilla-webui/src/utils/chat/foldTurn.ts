import type {
  ChatRenderedMessage,
  ChatStreamSegment,
  ChatStreamTimelineItem,
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
import type { Frame } from '@/types/turnlog'
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
  toolTimes: Map<string, { startedAt: number; endedAt?: number }>
  // Derived parts (reuse toParts/toSources, do not reimplement):
  parts: ChatPart[]
  sources: SourcePart[]
  // Accepted activity-phase transitions, in arrival order, for the finished
  // turn's activity timeline. Empty when no status frames were appended.
  statusHistory: StatusPart[]
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
 * A strict extension is still an ordinary suffix and therefore stays after the
 * last streamed segment. A conflicting snapshot supersedes every streamed text
 * segment, but keeps tool groups in arrival order and places the one canonical
 * text segment after them. An empty snapshot intentionally clears text while
 * retaining those tool groups.
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
    if (last?.type === 'text') {
      next[next.length - 1] = {
        ...last,
        raw: `${last.raw || ''}${suffix}`,
        dirty: true,
      }
    } else {
      next.push({ type: 'text', raw: suffix, html: '', dirty: true })
    }
    return { rawText: snapshot, segments: next, changed: true }
  }

  const next = segments.filter(segment => segment.type === 'tool-group')
  if (snapshot) next.push({ type: 'text', raw: snapshot, html: '', dirty: true })
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
  let rawText = ''
  let finalText: string | null = null
  let thinkingText = ''
  let toolGroupSeq = 0

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
        if (!lastSegment || lastSegment.type !== 'text') {
          segments.push({ type: 'text', raw: frame.text, html: '', dirty: true })
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
      case 'thinking': {
        thinkingText += frame.text
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
        // Append-only, already in accept order: setStreamActivity emits a frame
        // only on a real phase change, so each entry is a distinct transition.
        statusHistory.push({ action: frame.action, label: frame.label, at: frame.at })
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

  const timelineItems = segmentsToTimelineItems(segments, toolCalls, ownerKey)
  const base = { timelineItems, toolCalls, artifacts, rawText }
  const rendered = asRenderedMessage(base)

  return {
    ...base,
    thinkingText,
    toolTimes,
    statusHistory,
    parts: toParts(rendered, renderMarkdown, toolCallGroups, ownerKey, interrupts, interruptState),
    sources: toSources(rendered),
  }
}
