import type { ChatStreamSegment } from '@/types/chat'
import { readableTextAggregate } from '@/utils/chat/textAggregate'

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

  // Final/history text may add paragraph separators between the same exact
  // streamed prose blocks. This is not an answer rewrite: keep the original
  // intermediate/answer markers and tool boundaries instead of appending the
  // entire aggregate as a second, misleading answer segment.
  const chunks = segments.filter(segment => segment.type === 'text').map(segment => segment.raw || '')
  if (chunks.length && snapshot === readableTextAggregate(chunks)) {
    return { rawText: snapshot, segments, changed: true }
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
