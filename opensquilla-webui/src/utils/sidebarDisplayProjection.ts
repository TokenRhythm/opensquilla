import type {
  SidebarSection,
  SidebarSectionFamily,
  SidebarSectionRow,
} from '@/composables/useSessions'

export type SidebarDisplayZone = 'pinned' | 'projects' | 'recents'

export interface SidebarDisplayRow extends SidebarSectionRow {
  displayZone: SidebarDisplayZone
  displayFamily: SidebarSectionFamily
  displayProjectName: string
}

export interface SidebarDisplayRecentSection {
  family: SidebarSectionFamily
  label: string
  rows: SidebarDisplayRow[]
}

export interface SidebarDisplayProjection {
  pinned: SidebarDisplayRow[]
  projects: SidebarDisplayRow[]
  recents: SidebarDisplayRecentSection[]
  projectCount: number
  recentCount: number
  allRows: SidebarDisplayRow[]
}

export function isSidebarSessionOrderable(row: SidebarSectionRow): boolean {
  if (row.rowKind !== 'session' || row.provisional) return false
  if (row.sessionKind === 'chat') return true
  return Boolean(
    row.pinned
    && (row.sessionKind === 'cron' || row.sessionKind === 'channel'),
  )
}

function belongsToProject(
  row: SidebarSectionRow,
  project: SidebarSectionRow | null,
): boolean {
  if (!project) return false
  if (project.workspaceId) return row.workspaceId === project.workspaceId
  return Boolean(project.workspace && row.workspace === project.workspace)
}

export function buildSidebarDisplayProjection(
  sections: readonly SidebarSection[],
  sessionOrder: readonly string[] = [],
): SidebarDisplayProjection {
  const pinned: SidebarDisplayRow[] = []
  const pinnedSourceIndex = new Map<string, number>()
  const projects: SidebarDisplayRow[] = []
  const recents: SidebarDisplayRecentSection[] = []
  let sourceIndex = 0

  for (const section of sections) {
    const recentRows: SidebarDisplayRow[] = []
    let activeProject: SidebarSectionRow | null = null

    for (const row of section.rows) {
      if (row.rowKind === 'workspace') {
        activeProject = section.family === 'chats' ? row : null
        projects.push({
          ...row,
          displayZone: 'projects',
          displayFamily: section.family,
          displayProjectName: row.title,
        })
        continue
      }
      if (row.rowKind === 'workspace-empty') continue

      const isProjectSession = section.family === 'chats'
        && belongsToProject(row, activeProject)
      if (!isProjectSession) activeProject = null

      const displayProjectName = isProjectSession && activeProject
        ? activeProject.title
        : ''
      const displayZone: SidebarDisplayZone = row.pinned
        ? 'pinned'
        : isProjectSession
          ? 'projects'
          : 'recents'
      const displayRow: SidebarDisplayRow = {
        ...row,
        depth: displayZone === 'pinned' ? 0 : row.depth,
        displayZone,
        displayFamily: section.family,
        displayProjectName,
      }

      if (displayZone === 'pinned') {
        pinnedSourceIndex.set(row.key, sourceIndex++)
        pinned.push(displayRow)
      } else if (displayZone === 'projects') {
        projects.push(displayRow)
      } else {
        recentRows.push(displayRow)
      }
    }

    if (recentRows.length > 0) {
      recents.push({ family: section.family, label: section.label, rows: recentRows })
    }
  }

  const orderIndex = new Map(sessionOrder.map((key, index) => [key, index]))
  pinned.sort((a, b) => {
    const aIndex = orderIndex.get(a.key)
    const bIndex = orderIndex.get(b.key)
    if (aIndex !== undefined && bIndex !== undefined) return aIndex - bIndex
    if (aIndex !== undefined) return -1
    if (bIndex !== undefined) return 1
    return (pinnedSourceIndex.get(a.key) ?? 0) - (pinnedSourceIndex.get(b.key) ?? 0)
  })

  const recentCount = recents.reduce((sum, section) => sum + section.rows.length, 0)
  const projectCount = projects.filter(row => row.rowKind === 'workspace').length
  return {
    pinned,
    projects,
    recents,
    projectCount,
    recentCount,
    allRows: [
      ...pinned,
      ...projects,
      ...recents.flatMap(section => section.rows),
    ],
  }
}

export function sidebarSessionOrderKeys(
  sections: readonly SidebarSection[],
  sessionOrder: readonly string[] = [],
): string[] {
  return buildSidebarDisplayProjection(sections, sessionOrder)
    .allRows
    .filter(isSidebarSessionOrderable)
    .map(row => row.key)
}
