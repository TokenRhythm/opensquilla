import { computed, type Ref } from 'vue'

export interface ReadinessSectionDetail {
  status?: string
  blocking?: boolean
  actionRequired?: boolean
  required?: boolean
  label?: string
  detail?: string
}

export interface ReadinessStatus {
  needsOnboarding?: boolean
  hasConfig?: boolean
  llmSource?: string
  sectionDetails?: Record<string, ReadinessSectionDetail>
}

/** Pure predicate shared by the Settings dialog and the sidebar banner. */
export function readinessNeedsAction(status: ReadinessStatus | null | undefined): boolean {
  if (!status) return false
  if (status.needsOnboarding) return true
  if (status.llmSource === 'missing_env') return true
  const details = status.sectionDetails || {}
  return Object.values(details).some((d) =>
    d.blocking || d.actionRequired || d.status === 'missing' || d.status === 'degraded')
}

/** Headline action count for the banner. */
export function readinessActionCount(status: ReadinessStatus | null | undefined): number {
  if (!status) return 0
  const details = status.sectionDetails || {}
  let n = Object.values(details).filter((d) =>
    d.blocking || d.actionRequired || d.status === 'missing' || d.status === 'degraded').length
  if (status.llmSource === 'missing_env' && !details.llm && !details.provider) n += 1
  if (status.needsOnboarding && n === 0) n = 1
  return n
}

export function useReadinessSummary(status: Ref<ReadinessStatus | null>) {
  const needsAction = computed(() => readinessNeedsAction(status.value))
  const actionCount = computed(() => readinessActionCount(status.value))
  return { needsAction, actionCount }
}

// ---------------------------------------------------------------------------
// Cross-component invalidation. Settings saves hot-apply config on the
// gateway, but there is no server push for it, so long-lived holders of an
// `onboarding.status` snapshot (the sidebar banner) would keep stale state
// until a full page reload. Saves signal through this module-scope registry;
// subscribers re-fetch on their own RPC handle.

type ReadinessListener = () => void

const readinessListeners = new Set<ReadinessListener>()

/** Subscribe to readiness invalidations; returns an unsubscribe function. */
export function onReadinessInvalidated(listener: ReadinessListener): () => void {
  readinessListeners.add(listener)
  return () => { readinessListeners.delete(listener) }
}

/** Signal that gateway config changed and readiness snapshots must re-fetch. */
export function invalidateReadiness(): void {
  for (const listener of Array.from(readinessListeners)) listener()
}
