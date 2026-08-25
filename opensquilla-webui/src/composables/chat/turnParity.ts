import type {
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallRenderItem,
} from '@/types/chat'
import type { InterruptViewState } from '@/types/parts'
import type { ArtifactPayload } from '@/types/rpc'
import type { FoldedTurn } from '@/utils/chat/foldTurn'
import { toolState } from '@/utils/chat/toParts'

// The legacy live render surface as plain values (refs already unwrapped). The
// composable owns the Vue refs; this module compares pure data so it can be
// exercised directly by unit tests without a render context.
export interface LegacyTurnSurface {
  timelineItems: ChatStreamTimelineItem[]
  rawText: string
  toolCalls: ChatToolCall[]
  artifacts: ArtifactPayload[]
  thinkingText: string
}

/**
 * Pure structural diff of a folded turn against the legacy live surface. Returns
 * a list of human-readable divergences; an empty list means the fold renders the
 * same turn the legacy mutators built.
 *
 * Tool calls are compared on the FULL `result`, not only the 200-char
 * `resultPreview`: two folds whose tool output differs past the truncation point
 * (long results are routine) are NOT equivalent, and comparing previews alone
 * lets that divergence pass silently. Both `result` and `resultPreview` are
 * checked so the message names whichever field actually drifted.
 */
export function diffFoldVsLegacy(
  fold: FoldedTurn,
  legacy: LegacyTurnSurface,
  interruptState?: ReadonlyMap<string, InterruptViewState>,
): string[] {
  const problems: string[] = []

  if (fold.rawText !== legacy.rawText) {
    problems.push(`rawText diverges: fold len=${fold.rawText.length} legacy len=${legacy.rawText.length}`)
  }
  if (fold.thinkingText !== legacy.thinkingText) {
    problems.push('thinkingText diverges from legacy')
  }

  const legacyArtifacts = legacy.artifacts
  if (fold.artifacts.length !== legacyArtifacts.length) {
    problems.push(`artifact count ${fold.artifacts.length} != ${legacyArtifacts.length}`)
  } else {
    for (let i = 0; i < legacyArtifacts.length; i++) {
      if (artifactKey(fold.artifacts[i]) !== artifactKey(legacyArtifacts[i])) {
        problems.push(`artifact ${i} identity diverges`)
      }
    }
  }

  const legacyCalls = legacy.toolCalls
  if (fold.toolCalls.length !== legacyCalls.length) {
    problems.push(`tool call count ${fold.toolCalls.length} != ${legacyCalls.length}`)
  } else {
    for (let i = 0; i < legacyCalls.length; i++) {
      const diverged = divergedToolCallFields(fold.toolCalls[i], legacyCalls[i])
      if (diverged.length) {
        problems.push(`tool call ${i} (${legacyCalls[i].toolId}) fields diverge: ${diverged.join(', ')}`)
      }
    }
  }

  compareTimeline(fold.timelineItems, legacy.timelineItems, problems)

  // Also assert the fold's parts[]/sources — the surface the ON-mode render
  // actually consumes — covers the fold's own timeline/tools/artifacts. Mirrors
  // the history fold's coverage contract so the live fold is held to the same
  // bar before render ever flips onto parts[].
  checkFoldedPartsParity(fold, problems, interruptState)

  return problems
}

/** Names of the tool-call fields that differ between fold and legacy, in field
 *  order. `result` is the full tool output; `resultPreview` is its 200-char
 *  truncation — both are compared so a divergence past char 200 is never lost. */
function divergedToolCallFields(a: ChatToolCall, b: ChatToolCall): string[] {
  const fields: string[] = []
  if (a.toolId !== b.toolId) fields.push('toolId')
  if (a.name !== b.name) fields.push('name')
  if (a.status !== b.status) fields.push('status')
  if (a.isError !== b.isError) fields.push('isError')
  if (a.isRunning !== b.isRunning) fields.push('isRunning')
  if (a.inputPreview !== b.inputPreview) fields.push('inputPreview')
  if (a.result !== b.result) fields.push('result')
  if (a.resultPreview !== b.resultPreview) fields.push('resultPreview')
  return fields
}

function artifactKey(artifact: ArtifactPayload): string {
  return String(artifact?.id || artifact?.key || artifact?.name || '')
}

// Assert the fold's derived parts[]/sources cover exactly the fold's own
// timeline/tools/artifacts. The field-level checks above prove fold≡legacy; this
// proves the parts[] the ON render consumes is itself well-formed before the
// consumer flips onto it.
export function checkFoldedPartsParity(
  fold: FoldedTurn,
  problems: string[],
  interruptState?: ReadonlyMap<string, InterruptViewState>,
): void {
  const parts = fold.parts ?? []

  // (1) text coverage: every timeline text key has a matching text part.
  const textPartKeys = new Set(parts.filter(p => p.type === 'text').map(p => p.key))
  const timelineTextKeys = new Set(
    fold.timelineItems.filter(item => item.type === 'text').map(item => item.key),
  )
  if (!sameSet(textPartKeys, timelineTextKeys)) {
    problems.push(`parts text keys diverge from timeline: parts=${[...textPartKeys].join(',')} timeline=${[...timelineTextKeys].join(',')}`)
  }

  // (2) tool coverage: part keys + callId + state match the originating calls.
  const expectedCalls: ChatToolCallRenderItem[] = fold.timelineItems.flatMap(
    item => (item.type === 'tool-group' ? item.group.calls : []),
  )
  const expectedToolKeys = multiset(expectedCalls.map(call => call.renderKey))
  const toolParts = parts.filter(p => p.type === 'tool')
  const actualToolKeys = multiset(toolParts.map(p => p.key))
  if (!sameMultiset(expectedToolKeys, actualToolKeys)) {
    problems.push('parts tool keys diverge from originating call renderKeys')
  }
  const callByKey = new Map(expectedCalls.map(call => [call.renderKey, call]))
  for (const part of toolParts) {
    if (part.type !== 'tool') continue
    const call = callByKey.get(part.key)
    if (!call) continue
    if (part.callId !== call.toolId) problems.push(`parts tool callId ${part.callId} != ${call.toolId}`)
    if (part.state !== toolState(call)) problems.push(`parts tool state ${part.state} != ${toolState(call)} for ${part.key}`)
  }

  // (3) artifact coverage.
  const artifactParts = parts.filter(p => p.type === 'artifact').length
  if (artifactParts !== fold.artifacts.length) {
    problems.push(`parts artifacts ${artifactParts} != artifacts ${fold.artifacts.length}`)
  }

  // (3a) interrupt coverage: keys are unique (one part per approval id), each
  // part carries exactly the payload its kind names, and resolution/busy/error
  // echo the interruptState side-map (or its defaults when unset).
  const interruptParts = parts.filter(p => p.type === 'interrupt')
  const interruptKeys = multiset(interruptParts.map(p => p.key))
  for (const [key, count] of interruptKeys) {
    if (count !== 1) problems.push(`parts interrupt key ${key} appears ${count}x`)
  }
  for (const part of interruptParts) {
    if (part.type !== 'interrupt') continue
    const id = part.key.split(':interrupt:').pop() ?? ''
    if (part.interruptKind === 'approval') {
      if (!part.approval) problems.push(`parts interrupt ${part.key} missing approval payload`)
      if (part.clarify) problems.push(`parts interrupt ${part.key} carries a clarify payload for an approval`)
    } else {
      if (!part.clarify) problems.push(`parts interrupt ${part.key} missing clarify payload`)
      if (part.approval) problems.push(`parts interrupt ${part.key} carries an approval payload for a clarify`)
    }
    const state = interruptState?.get(id)
    const expectedResolution = state?.resolution ?? null
    const expectedBusy = state?.busy ?? false
    const expectedError = state?.error ?? ''
    if (part.resolution !== expectedResolution) {
      problems.push(`parts interrupt ${part.key} resolution ${part.resolution} != ${expectedResolution}`)
    }
    if (part.busy !== expectedBusy) {
      problems.push(`parts interrupt ${part.key} busy ${part.busy} != ${expectedBusy}`)
    }
    if (part.error !== expectedError) {
      problems.push(`parts interrupt ${part.key} error diverges from interruptState`)
    }
  }

  // (4) sources: folded list stays within the cap and keeps monotonic ids.
  const sources = fold.sources ?? []
  if (sources.length > 12) problems.push(`sources ${sources.length} exceeds MAX_SOURCES`)
  sources.forEach((source, index) => {
    if (source.sourceId !== index + 1) problems.push(`source ${index} has sourceId ${source.sourceId}`)
  })

  // (5) statusHistory: append-only, monotonic non-decreasing timestamps, every
  // entry carries a stable action key + label. The live fold has no legacy ref
  // to diff against (like interrupts), so we assert the fold's own invariants.
  const history = fold.statusHistory ?? []
  let prevAt = -Infinity
  for (const entry of history) {
    if (!entry.action) problems.push('status entry missing action key')
    if (typeof entry.at !== 'number' || entry.at < prevAt) {
      problems.push(`status entry timestamp out of order: ${entry.at} < ${prevAt}`)
    }
    prevAt = entry.at
  }
}

function multiset(values: string[]): Map<string, number> {
  const map = new Map<string, number>()
  for (const value of values) map.set(value, (map.get(value) ?? 0) + 1)
  return map
}

function sameMultiset(a: Map<string, number>, b: Map<string, number>): boolean {
  if (a.size !== b.size) return false
  for (const [key, count] of a) if (b.get(key) !== count) return false
  return true
}

function sameSet(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false
  for (const value of a) if (!b.has(value)) return false
  return true
}

function compareTimeline(
  fold: ChatStreamTimelineItem[],
  legacy: ChatStreamTimelineItem[],
  problems: string[],
): void {
  if (fold.length !== legacy.length) {
    problems.push(`timeline length ${fold.length} != ${legacy.length}`)
    return
  }
  for (let i = 0; i < legacy.length; i++) {
    const a = fold[i]
    const b = legacy[i]
    if (a.type !== b.type || a.key !== b.key) {
      problems.push(`timeline item ${i} type/key diverges (${a.type}:${a.key} vs ${b.type}:${b.key})`)
      continue
    }
    // Text-item html is intentionally NOT compared: the legacy flush renders
    // markdown ~80ms behind the accepted frame (flushRender), so a freshly
    // folded html would spuriously diverge from the still-stale legacy html
    // during that window. Whole-turn rawText equality (above) plus type+key
    // parity here covers text correctness.
    if (a.type === 'text' && b.type === 'text' && a.presentation !== b.presentation) {
      problems.push(`timeline text ${i} presentation diverges`)
    }
    if (a.type === 'tool-group' && b.type === 'tool-group') {
      if (a.group.groupId !== b.group.groupId) {
        problems.push(`timeline tool-group ${i} groupId diverges`)
      }
      if (a.group.calls.length !== b.group.calls.length) {
        problems.push(`timeline tool-group ${i} call count diverges`)
        continue
      }
      for (let c = 0; c < b.group.calls.length; c++) {
        const ac = a.group.calls[c]
        const bc = b.group.calls[c]
        if (ac.toolId !== bc.toolId) {
          problems.push(`timeline tool-group ${i} call ${c} toolId diverges`)
        }
        if (toolState(ac) !== toolState(bc)) {
          problems.push(`timeline tool-group ${i} call ${c} state diverges`)
        }
      }
    }
  }
}
