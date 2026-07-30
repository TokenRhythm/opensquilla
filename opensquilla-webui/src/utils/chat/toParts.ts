import type {
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallGroup,
  ChatToolCallRenderItem,
} from '@/types/chat'
import type {
  ChatPart,
  InterruptApprovalData,
  InterruptClarifyData,
  InterruptViewState,
  ToolPartState,
} from '@/types/parts'
import { toolOperationKey } from '@/utils/chat/toolDisplay'

/** One ordered interrupt the fold hands to `toParts` (kept structural to avoid a
 *  cycle with foldTurn, which imports this module). */
export interface ToPartsInterrupt {
  kind: 'approval' | 'clarify'
  approvalId: string
  data: InterruptApprovalData | InterruptClarifyData
}

function assertNever(x: never): never {
  throw new Error(`unhandled: ${JSON.stringify(x)}`)
}

export function toolState(call: Pick<ChatToolCall, 'isRunning' | 'status' | 'result' | 'isError'>): ToolPartState {
  if (call.isError || call.status === 'error') return 'output-error'
  if (call.status === 'success') return 'output-available'
  if (call.isRunning) return 'input-available'
  return 'input-streaming' // no result yet, not running, not errored (history never hits this)
}

export function toolPartFromCall(
  call: ChatToolCall,
  groupId: string,
  operationKey: string,
  key: string,
): ChatPart {
  return {
    type: 'tool',
    callId: call.toolId,
    groupId,
    operationKey: operationKey || toolOperationKey(call.name),
    toolName: call.name,
    displayName: call.displayName,
    state: toolState(call),
    isRunning: call.isRunning,
    status: call.status,
    isError: call.isError,
    input: call.inputRaw ?? '',
    inputPreview: call.inputPreview,
    output: call.result,
    outputPreview: call.resultPreview,
    error: (call.isError || call.status === 'error') ? call.result : undefined,
    key,
  }
}

function pushTimelineItem(parts: ChatPart[], item: ChatStreamTimelineItem) {
  if (item.type === 'text') {
    parts.push({ type: 'text', html: item.html, rawText: item.rawText ?? '', key: item.key })
    return
  }
  if (item.type === 'tool-group') {
    // tool-group → one 'tool' part per call; keep the call's renderKey as the part key
    for (const call of item.group.calls) {
      parts.push(toolPartFromCall(call, item.group.groupId, item.group.operationKey, call.renderKey))
    }
    return
  }
  if (item.type === 'interrupt') {
    parts.push(item.part)
    return
  }
  return assertNever(item)
}

/**
 * Pure ordered fold of a rendered assistant message into a discriminated
 * `parts[]` list, walking the same render order the message components use
 * today (reasoning, then timeline-XOR-text body, then artifacts). It consumes
 * the already-normalized `ChatRenderedMessage` (not raw payloads) and reuses the
 * pre-rendered, already-sanitized `item.html`. Sources are folded separately
 * (see toSources); nothing renders these parts yet.
 *
 * `toolCallGroups` is injected (not imported) so the legacy no-timeline path
 * shares the exact helper the message components use, avoiding a divergent key
 * scheme. `ownerKey` is passed in (rather than re-derived here) so part keys use
 * the identical owner the composable and the message components key against,
 * which keeps tool renderKeys aligned even when `messageId` is absent.
 */
export function toParts(
  msg: ChatRenderedMessage,
  renderMarkdown: (text: string) => string,
  toolCallGroups: (calls: ChatToolCall[] | undefined, baseKey: string) => ChatToolCallGroup[],
  ownerKey: string,
  interrupts: readonly ToPartsInterrupt[] = [],
  interruptState: ReadonlyMap<string, InterruptViewState> = new Map(),
): ChatPart[] {
  const parts: ChatPart[] = []

  // (1) reasoning — only assistants carry it (the composable gates this upstream)
  if (msg.reasoning) {
    parts.push({
      type: 'reasoning',
      text: msg.reasoning.text,
      seconds: msg.reasoning.seconds,
      key: `${ownerKey}:reasoning`,
    })
  }

  // (2) body: timelineItems XOR text, exactly as the assistant message render order
  if (msg.timelineItems?.length) {
    for (const item of msg.timelineItems) pushTimelineItem(parts, item)
  } else {
    // 2a. plain text (only when no timeline) — reuse the html the component would render
    if (msg.text) {
      parts.push({
        type: 'text',
        html: renderMarkdown(msg.text),
        rawText: msg.text,
        key: `${ownerKey}:text`,
      })
    }
    // 2b. legacy group-only timeline from toolCalls; build groups with the SAME
    // key scheme the component's legacyTimelineItems uses (toolCallGroups + the
    // injected ownerKey), then flatten each group into per-call 'tool' parts.
    if (msg.toolCalls?.length) {
      for (const group of toolCallGroups(msg.toolCalls, ownerKey)) {
        for (const call of group.calls as ChatToolCallRenderItem[]) {
          parts.push(toolPartFromCall(call, group.groupId, group.operationKey, call.renderKey))
        }
      }
    }
  }

  // (2c) interrupts — after the body, before artifacts: an approval blocks the
  // run mid-stream, so it belongs after the text/tools that preceded it and
  // before the turn's final deliverables. One part per id, in arrival order.
  const timelineInterruptIds = new Set(
    msg.timelineItems
      ?.filter(
        (item): item is Extract<ChatStreamTimelineItem, { type: 'interrupt' }> =>
          item.type === 'interrupt',
      )
      .map(item => item.approvalId) ?? [],
  )
  for (const it of interrupts) {
    if (timelineInterruptIds.has(it.approvalId)) continue
    const state = interruptState.get(it.approvalId)
    parts.push({
      type: 'interrupt',
      interruptKind: it.kind,
      approval: it.kind === 'approval' ? (it.data as InterruptApprovalData) : undefined,
      clarify: it.kind === 'clarify' ? (it.data as InterruptClarifyData) : undefined,
      resolution: state?.resolution ?? null,
      busy: state?.busy ?? false,
      error: state?.error ?? '',
      key: `${ownerKey}:interrupt:${it.approvalId}`,
    })
  }

  // (2d) immutable plan revisions are typed transcript parts. The rendered
  // message suppresses their Markdown fallback body, so this card is the one
  // visual representation while the raw text remains available to providers
  // and older clients.
  for (const plan of msg.planRevisions ?? []) {
    parts.push({
      type: 'plan',
      plan,
      key: `${ownerKey}:plan:${plan.revisionId}`,
    })
  }

  // (3) artifacts — one part each, in order
  for (const artifact of msg.artifacts ?? []) {
    const id = String(artifact.id || artifact.key || artifact.name || parts.length)
    parts.push({ type: 'artifact', artifact, key: `${ownerKey}:artifact:${id}` })
  }

  return parts
}
