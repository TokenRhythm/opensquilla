const expandedByRevision = new Map<string, boolean>()

function revisionKey(planId: string, revisionId: string): string {
  return `${planId}:${revisionId}`
}

export function readPlanDisclosureExpansion(
  planId: string,
  revisionId: string,
  fallback = false,
): boolean {
  return expandedByRevision.get(revisionKey(planId, revisionId)) ?? fallback
}

export function writePlanDisclosureExpansion(
  planId: string,
  revisionId: string,
  expanded: boolean,
): void {
  expandedByRevision.set(revisionKey(planId, revisionId), expanded)
}

export function clearPlanDisclosureExpansionState(): void {
  expandedByRevision.clear()
}
