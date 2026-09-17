import type {
  ChatMessage,
  ChatModelCallSegment,
  ChatUsagePayload,
} from '@/types/chat'
import {
  normalizeModelCallSegments,
  type NormalizedModelCallSegment,
} from '@/utils/chat/modelCallSegments'

function usageSegments(usage: ChatUsagePayload | undefined): ChatModelCallSegment[] {
  const value = usage?.model_call_segments ?? usage?.modelCallSegments
  return Array.isArray(value) ? value : []
}

function matchesSegment(message: ChatMessage, segment: NormalizedModelCallSegment): boolean {
  if (
    message.role !== 'user'
    || message.inputDisposition !== 'applied'
    || message.steerModelCallId !== segment.modelCallId
  ) {
    return false
  }
  return message.steerAppliedIteration === undefined
    || message.steerAppliedIteration === segment.iteration
}

function syntheticAssistantSegment(
  source: ChatMessage,
  text: string,
  segmentKey: string,
  projectRouter = false,
): ChatMessage {
  const routerUsage = projectRouter ? source.usage || source.turn_usage : undefined
  const routerModelCallId = String(
    routerUsage?.router_model_call_id || routerUsage?.routerModelCallId || '',
  ).trim()
  const routerIteration = Number(
    routerUsage?.router_iteration || routerUsage?.routerIteration || 0,
  ) || 0
  return {
    role: 'assistant',
    text,
    ts: source.ts,
    clientId: `history-model-call-segment:${source.messageId || source.clientId || 'assistant'}:${segmentKey}`,
    turnId: source.turnId,
    restoredFromHistory: true,
    ...(routerUsage ? { routerUsage } : {}),
    ...(routerModelCallId ? { routerModelCallId } : {}),
    ...(routerIteration ? { routerIteration } : {}),
  }
}

function interleaveAssistantAt(
  messages: ChatMessage[],
  assistantIndex: number,
): ChatMessage[] | null {
  const assistant = messages[assistantIndex]
  if (
    !assistant
    || assistant.role !== 'assistant'
    || !assistant.turnId
    || !assistant.text
  ) {
    return null
  }
  const codepoints = Array.from(assistant.text)
  const segments = normalizeModelCallSegments(
    usageSegments(assistant.usage || assistant.turn_usage),
    codepoints.length,
  )
  if (segments.length === 0) return null

  const matchedByCall = new Map<string, Array<{ index: number; message: ChatMessage }>>()
  for (const segment of segments) matchedByCall.set(segment.modelCallId, [])
  for (let index = 0; index < assistantIndex; index++) {
    const message = messages[index]
    if (message?.turnId !== assistant.turnId) continue
    const segment = segments.find(candidate => matchesSegment(message, candidate))
    if (!segment) continue
    matchedByCall.get(segment.modelCallId)!.push({ index, message })
  }
  if ([...matchedByCall.values()].some(rows => rows.length === 0)) return null

  const segmentRowIndexes = segments.map(segment =>
    matchedByCall.get(segment.modelCallId)!.map(row => row.index),
  )
  let previousIndex = -1
  for (const indexes of segmentRowIndexes) {
    if (indexes[0]! <= previousIndex) return null
    previousIndex = indexes[indexes.length - 1]!
  }
  const firstSteerIndex = segmentRowIndexes[0]![0]!
  const matchedIndexes = new Set(segmentRowIndexes.flat())

  // Fail closed if another durable row sits inside the aggregate block. The
  // transform must never move unrelated history merely to obtain a prettier
  // ordering.
  for (let index = firstSteerIndex; index < assistantIndex; index++) {
    if (!matchedIndexes.has(index)) return null
  }

  const replacement: ChatMessage[] = []
  const firstStart = segments[0]!.startCodepoint
  if (firstStart > 0) {
    replacement.push(
      syntheticAssistantSegment(
        assistant,
        codepoints.slice(0, firstStart).join(''),
        `prefix-${firstStart}`,
        true,
      ),
    )
  }
  segments.forEach((segment, segmentIndex) => {
    replacement.push(
      ...matchedByCall.get(segment.modelCallId)!.map(row => row.message),
    )
    const text = codepoints
      .slice(segment.startCodepoint, segment.endCodepoint)
      .join('')
    const isLast = segmentIndex === segments.length - 1
    if (isLast) {
      replacement.push({ ...assistant, text })
    } else if (text) {
      replacement.push(
        syntheticAssistantSegment(
          assistant,
          text,
          `${segment.modelCallId}-${segment.startCodepoint}-${segment.endCodepoint}`,
        ),
      )
    }
  })

  return [
    ...messages.slice(0, firstSteerIndex),
    ...replacement,
    ...messages.slice(assistantIndex + 1),
  ]
}

/**
 * Reconstruct the visible same-turn chronology from one aggregated assistant
 * transcript row plus durable steer application metadata.
 */
export function interleaveHistoryModelCallSegments(
  messages: ChatMessage[],
): ChatMessage[] {
  let result = messages
  for (let index = 0; index < result.length; index++) {
    const transformed = interleaveAssistantAt(result, index)
    if (!transformed) continue
    result = transformed
    // The canonical assistant row has moved to the end of the replacement.
    // Rescanning it is harmless (its shortened text fails range validation),
    // and lets later turns in the same page be transformed as well.
  }
  return result
}
