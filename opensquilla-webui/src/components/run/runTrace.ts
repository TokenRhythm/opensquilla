import type { NodeStep, RunTraceSummary } from '@/types/runTrace'
import type {
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import type { ChatHistoryMessage } from '@/types/rpc'
import type { ToolPartState } from '@/types/parts'
import { toolState } from '@/utils/chat/toParts'
import {
  isInternalToolName,
  normalizeToolName,
  normalizeToolInputText,
  toolOperationKey,
  toolDisplayName,
  truncateToolPreview,
} from '@/utils/chat/toolDisplay'

/** status enum → tone + glyph. Returns CSS-class suffixes only (never a raw
 *  colour); the SFC owns the var() bindings, so the colour guard stays happy. */
export interface StatusVisual {
  tone: 'running' | 'ok' | 'err' | 'idle'
  glyph: 'check' | 'x' | null
}
export function statusVisual(state: ToolPartState): StatusVisual {
  switch (state) {
    case 'output-error':     return { tone: 'err',     glyph: 'x' }
    case 'output-available': return { tone: 'ok',      glyph: 'check' }
    case 'input-available':  return { tone: 'running', glyph: null }  // dispatched, awaiting
    case 'input-streaming':  return { tone: 'idle',    glyph: null }
  }
}

/** Flatten the chat timeline's nested groups into flat NodeStep[]. Each call
 *  becomes a step; group identity rides on parentId (a synthetic group id) so
 *  composeTree() re-derives the group header for ≥2 members. Used only if a
 *  surface wants the flat shape from a chat message; the chat wrapper itself
 *  passes `items` directly and never calls this. */
export function nodeStepsFromTimeline(items: ChatStreamTimelineItem[]): NodeStep[] {
  const steps: NodeStep[] = []
  for (const item of items) {
    if (item.type !== 'tool-group') continue
    const group = item.group
    const parentId = group.calls.length > 1 ? group.groupId : null
    for (const call of group.calls) {
      steps.push(stepFromRenderItem(call, group.operationKey, parentId))
    }
  }
  return steps
}

function stepFromRenderItem(
  call: ChatToolCallRenderItem,
  operationKey: string,
  parentId: string | null,
): NodeStep {
  return {
    id: call.renderKey,
    parentId,
    title: call.displayName,
    toolName: call.name,
    operationKey: operationKey || toolOperationKey(call.name),
    state: toolState(call),
    elapsedMs: null,
    input: call.inputRaw ?? '',
    inputPreview: call.inputPreview,
    output: call.result,
    outputPreview: call.resultPreview,
    isError: call.isError,
  }
}

/** Build flat NodeStep[] from a raw history row's tool_calls, mirroring
 *  SessionInspect's toolPills id-merge (SessionInspectDrawer.vue:207-223) but
 *  producing a full NodeStep (state/input/output/isError). MUST drop internal
 *  tools via isInternalToolName — toolPills does not, but RunTrace shows
 *  input/output so internals must not leak. Unnamed entries (normalizeToolName
 *  returns '') are skipped, matching the chat path. */
export function nodeStepsFromHistoryMessage(msg: ChatHistoryMessage): NodeStep[] {
  if (!Array.isArray(msg.tool_calls)) return []

  interface Pending {
    id: string
    name: string
    input: string
    output: string
    isError: boolean
  }
  const byId = new Map<string, Pending>()
  const order: string[] = []
  let anonymous = 0

  for (const entry of msg.tool_calls) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const record = entry as Record<string, unknown>
    const type = String(record.type || '')
    if (type && type !== 'tool_use' && type !== 'tool_result') continue

    const id = String(record.tool_use_id || record.toolId || record.id || '') || `anon-${anonymous++}`
    const input = normalizeToolInputText(record)
    const rawResult = record.result ?? record.content ?? record.output ?? ''
    const output = typeof rawResult === 'string' ? rawResult : JSON.stringify(rawResult, null, 2)
    const executionStatus = String(
      ((record.execution_status as Record<string, unknown> | undefined)?.status) ?? '',
    )
    const isError = !!(
      record.is_error || record.isError || record.error
      || ['error', 'timeout', 'cancelled'].includes(executionStatus)
    )

    let pending = byId.get(id)
    if (!pending) {
      pending = { id, name: '', input: '', output: '', isError: false }
      byId.set(id, pending)
      order.push(id)
    }
    const name = normalizeToolName(record)
    if (name) pending.name = name
    if (input) pending.input = input
    if (output) pending.output = output
    if (isError) pending.isError = true
  }

  const steps: NodeStep[] = []
  for (const id of order) {
    const pending = byId.get(id)
    if (!pending) continue
    const name = pending.name
    if (!name || isInternalToolName(name)) continue
    const output = pending.output
    const isError = pending.isError
    const status: '' | 'success' | 'error' = isError
      ? 'error'
      : (output ? 'success' : '')
    steps.push({
      id: pending.id,
      parentId: null,
      title: toolDisplayName(name, pending.input),
      toolName: name,
      operationKey: toolOperationKey(name),
      // History is always terminal — no live running state (toParts.ts:9).
      state: toolState({ isRunning: false, status, result: output, isError }),
      elapsedMs: null,
      input: pending.input,
      inputPreview: truncateToolPreview(pending.input),
      output,
      outputPreview: truncateToolPreview(output),
      isError,
    })
  }
  return steps
}

/** Compose flat NodeStep[] → render tree (group → members). Single pass: steps
 *  sharing a synthetic group parentId nest under one header; standalone steps
 *  stay roots. A group with exactly one member renders as a single row (matches
 *  ToolCallTimeline.vue:24/68 `calls.length > 1` branch). */
export interface TraceNode { step: NodeStep; children: TraceNode[] }
export function composeTree(steps: NodeStep[]): TraceNode[] {
  const roots: TraceNode[] = []
  const groupNodes = new Map<string, TraceNode>()

  for (const step of steps) {
    const parentId = step.parentId
    if (!parentId) {
      roots.push({ step, children: [] })
      continue
    }
    let group = groupNodes.get(parentId)
    if (!group) {
      // A synthetic group header carries the first member's identity until a
      // surface supplies a real header step; members nest beneath it.
      group = { step: { ...step, id: parentId }, children: [] }
      groupNodes.set(parentId, group)
      roots.push(group)
    }
    group.children.push({ step, children: [] })
  }

  return roots
}

// `RunTraceSummary` is re-exported for callers composing a summary strip from
// the same module that derives the steps.
export type { RunTraceSummary }
