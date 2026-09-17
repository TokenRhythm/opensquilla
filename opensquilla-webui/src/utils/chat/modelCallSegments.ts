import type { ChatModelCallSegment } from '@/types/chat'

export interface NormalizedModelCallSegment {
  modelCallId: string
  iteration: number
  startCodepoint: number
  endCodepoint: number
}

function nonNegativeInteger(value: unknown): number | undefined {
  const number = Number(value)
  return Number.isInteger(number) && number >= 0 ? number : undefined
}

function positiveInteger(value: unknown): number | undefined {
  const number = nonNegativeInteger(value)
  return number !== undefined && number > 0 ? number : undefined
}

/**
 * Validate the backend's model-call ranges before they are allowed to move or
 * split visible messages. Invalid or partial metadata deliberately fails
 * closed so an older gateway cannot corrupt the local chronology.
 */
export function normalizeModelCallSegments(
  rawSegments: ChatModelCallSegment[] | null | undefined,
  codepointLength: number,
): NormalizedModelCallSegment[] {
  if (!Array.isArray(rawSegments) || rawSegments.length === 0) return []

  const seenCallIds = new Set<string>()
  const normalized: NormalizedModelCallSegment[] = []
  for (const raw of rawSegments) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return []
    const modelCallId = String(raw.model_call_id || raw.modelCallId || '').trim()
    const iteration = positiveInteger(raw.iteration)
    const startCodepoint = nonNegativeInteger(
      raw.start_codepoint ?? raw.startCodepoint,
    )
    const endCodepoint = nonNegativeInteger(raw.end_codepoint ?? raw.endCodepoint)
    if (
      !modelCallId
      || seenCallIds.has(modelCallId)
      || iteration === undefined
      || startCodepoint === undefined
      || endCodepoint === undefined
      || endCodepoint < startCodepoint
      || endCodepoint > codepointLength
      || (
        normalized.length > 0
        && startCodepoint !== normalized[normalized.length - 1]!.endCodepoint
      )
    ) {
      return []
    }
    seenCallIds.add(modelCallId)
    normalized.push({ modelCallId, iteration, startCodepoint, endCodepoint })
  }

  if (normalized[normalized.length - 1]!.endCodepoint !== codepointLength) {
    return []
  }
  return normalized
}

/** Return the pre-steer prefix followed by one text chunk per model call. */
export function splitTextByModelCallSegments(
  text: string,
  rawSegments: ChatModelCallSegment[] | null | undefined,
): string[] | null {
  const codepoints = Array.from(text)
  const segments = normalizeModelCallSegments(rawSegments, codepoints.length)
  if (segments.length === 0) return null

  return [
    codepoints.slice(0, segments[0]!.startCodepoint).join(''),
    ...segments.map(segment =>
      codepoints.slice(segment.startCodepoint, segment.endCodepoint).join(''),
    ),
  ]
}
