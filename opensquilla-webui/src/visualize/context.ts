import type { VisualizeContext, VisualizeJson } from './types'
import { createVisualizeSnapshotStore, type VisualizeSnapshotRevision } from './snapshotStore'

export const VISUALIZE_CONTEXT_MAX_BYTES = 8 * 1024
export const VISUALIZE_CONTEXT_SUMMARY_MAX = 400
export const VISUALIZE_CONTEXT_PROMPT_MAX = 2000

/** Copy bounded plain JSON before persisting it or adding it to a composer. */
export function normalizeVisualizeContext(input: unknown): VisualizeContext | null {
  if (input === null) return null
  const plain = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object'
    && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null))
  if (!plain(input) || Object.keys(input).some(key => !['summary', 'data', 'suggestedPrompt'].includes(key))) {
    throw new Error('Canvas context must contain only summary, data and suggestedPrompt')
  }
  if (typeof input.summary !== 'string' || input.summary.length > VISUALIZE_CONTEXT_SUMMARY_MAX || !input.summary.trim()) {
    throw new Error(`Canvas context summary must contain 1–${VISUALIZE_CONTEXT_SUMMARY_MAX} characters`)
  }
  if ('suggestedPrompt' in input && (typeof input.suggestedPrompt !== 'string' || input.suggestedPrompt.length > VISUALIZE_CONTEXT_PROMPT_MAX)) {
    throw new Error(`Canvas suggestedPrompt must be a string of at most ${VISUALIZE_CONTEXT_PROMPT_MAX} characters`)
  }
  let nodes = 0
  const copy = (value: unknown, depth: number): VisualizeJson => {
    if (++nodes > 2048 || depth > 8) throw new Error('Canvas context data is too complex')
    if (value === null || typeof value === 'boolean') return value
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string' && value.length <= VISUALIZE_CONTEXT_MAX_BYTES) return value
    if (Array.isArray(value)) return Array.from(value, item => copy(item, depth + 1))
    if (plain(value)) {
      const result: { [key: string]: VisualizeJson } = Object.create(null)
      for (const key of Object.keys(value)) {
        if (key.length > VISUALIZE_CONTEXT_MAX_BYTES) throw new Error('Canvas context data is too large')
        result[key] = copy(value[key], depth + 1)
      }
      return result
    }
    throw new Error('Canvas context data must be finite plain JSON')
  }
  const result: VisualizeContext = { summary: input.summary.trim() }
  if ('data' in input) result.data = copy(input.data, 0)
  if (typeof input.suggestedPrompt === 'string') result.suggestedPrompt = input.suggestedPrompt.trim()
  const encoded = JSON.stringify(result)
  if (new TextEncoder().encode(encoded).length > VISUALIZE_CONTEXT_MAX_BYTES) throw new Error('Canvas context exceeds the 8 KiB limit')
  return JSON.parse(encoded) as VisualizeContext
}

const snapshots = createVisualizeSnapshotStore<VisualizeContext | null>({
  prefix: 'opensquilla.visualize-context.v2.', legacyPrefix: 'opensquilla.visualize-context.v1.',
  maxValueLength: VISUALIZE_CONTEXT_MAX_BYTES, normalize: normalizeVisualizeContext,
})

export function hasNewerVisualizeContext(key: string, revision: VisualizeSnapshotRevision): boolean {
  return snapshots.hasNewer(key, revision)
}

// Clearing conditions retains their host version fence; no wrapper reaches the guest.
export function readVisualizeContext(key: string, revision?: VisualizeSnapshotRevision): VisualizeContext | null {
  return snapshots.read(key, revision) ?? null
}

export function saveVisualizeContext(key: string, value: VisualizeContext | null, revision?: VisualizeSnapshotRevision): void {
  const context = normalizeVisualizeContext(value)
  snapshots.save(key, context, revision)
}
