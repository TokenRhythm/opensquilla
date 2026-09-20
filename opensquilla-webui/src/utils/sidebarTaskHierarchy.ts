import type { SidebarSectionRow } from '@/composables/useSessions'

export interface SidebarSubtaskSummary {
  count: number
  running: number
  attention: number
  allFinished: boolean
}

const FINISHED_STATUSES = new Set(['idle', 'completed', 'complete', 'succeeded', 'success', 'cancelled'])
const ATTENTION_STATUSES = new Set(['failed', 'timeout', 'interrupted', 'abandoned'])

/** Keep lineage independent of capped indentation and pinned display zones. */
export function buildSidebarTaskHierarchy(rows: readonly SidebarSectionRow[]) {
  const byKey = new Map(rows.filter(row => row.rowKind === 'session').map(row => [row.key, row]))
  const ancestors = new Map<string, string[]>()
  const summaries = new Map<string, SidebarSubtaskSummary>()

  for (const row of byKey.values()) {
    const lineage: string[] = []
    const seen = new Set([row.key])
    let parentKey = row.parentKey
    while (parentKey && byKey.has(parentKey) && !seen.has(parentKey)) {
      seen.add(parentKey)
      lineage.push(parentKey)
      parentKey = byKey.get(parentKey)?.parentKey
    }
    ancestors.set(row.key, lineage)
    const running = row.taskAttention === 'running' || ['queued', 'running'].includes(row.runStatus)
    const attention = row.taskAttention === 'failed' || ATTENTION_STATUSES.has(row.runStatus)
    for (const ancestor of lineage) {
      const summary = summaries.get(ancestor) ?? { count: 0, running: 0, attention: 0, allFinished: true }
      summary.count++
      summary.running += Number(running)
      summary.attention += Number(attention)
      summary.allFinished &&= !running && !attention && FINISHED_STATUSES.has(row.runStatus)
      summaries.set(ancestor, summary)
    }
  }

  return { ancestors, summaries }
}
