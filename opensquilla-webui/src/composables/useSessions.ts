import { ref, computed } from 'vue'
import i18n from '@/i18n'
import type { ProjectWorkspaceItem } from '@/types/rpc'
import type { SessionTaskAttention } from '@/composables/useSessionTaskAttention'
import type { ChatRunStatusSource } from '@/types/chat'
import type {
  SessionDirectory,
  SessionItem,
} from '@/modules/sessionDirectory'
import { sessionRunStatusLabel, summarizeSessionTask } from '@/modules/sessionRunStatus'

const SESSION_LIST_PAGE_SIZE = 200

export type { SessionItem } from '@/modules/sessionDirectory'

export interface SessionGroup {
  label: string
  items: SessionItem[]
  updatedAt: number
}

export function runStatusLabelText(
  status: string,
  source?: ChatRunStatusSource,
): string {
  return sessionRunStatusLabel(
    status,
    summarizeSessionTask(source?.last_task || source?.lastTask),
  )
}

export function sessionParentKey(item: SessionItem): string {
  return item.parent?.key || ''
}

function isSubagentSession(item: SessionItem): boolean {
  return item.sessionKind === 'task' || item.surface === 'subagent'
}

export function sessionSpawnDepth(item: SessionItem): number {
  return item.parent && item.parent.spawnDepth > 0 ? item.parent.spawnDepth : 0
}

export interface SessionLedgerEntry {
  item: SessionItem
  depth: number
  parentTitle: string
}

/** Flatten subagent sessions beneath their parent while preserving recency. */
export function arrangeSessionLedger(items: SessionItem[]): SessionLedgerEntry[] {
  const byKey = new Map(items.map(item => [item.key, item]))
  const children = new Map<string, SessionItem[]>()
  const roots: SessionItem[] = []
  for (const item of items) {
    const parentKey = sessionParentKey(item)
    if (isSubagentSession(item) && parentKey && parentKey !== item.key && byKey.has(parentKey)) {
      const list = children.get(parentKey) || []
      list.push(item)
      children.set(parentKey, list)
    } else {
      roots.push(item)
    }
  }
  const entries: SessionLedgerEntry[] = []
  const visit = (item: SessionItem, depth: number, parentTitle: string) => {
    entries.push({ item, depth, parentTitle: depth > 0 ? parentTitle : '' })
    for (const child of children.get(item.key) || []) {
      visit(child, Math.min(depth + 1, 3), item.title)
    }
  }
  for (const root of roots) {
    // An orphan subagent (parent not in the visible list) still indents when
    // the contract marks it spawned. The published parent projection carries
    // identity/depth, not a title, so an absent parent has no lineage label.
    const orphanDepth = isSubagentSession(root) && sessionSpawnDepth(root) > 0 ? 1 : 0
    visit(root, orphanDepth, '')
  }
  return entries
}

export type SidebarSectionFamily = 'chats' | 'channels' | 'automations'

export interface SidebarSectionRow {
  rowKind: 'session' | 'workspace' | 'workspace-empty'
  key: string
  title: string
  effectiveAgentId: string
  agentName: string
  sessionKind: string
  depth: number
  runStatus: string
  runLabel: string
  taskAttention: SessionTaskAttention
  updatedAt: number
  hasContractGaps: boolean
  workspace?: string
  workspaceId?: string
  workspaceLabel?: string
  workspaceDisplayPath?: string
  workspaceTaskCount?: number
  workspacePinned?: boolean
  workspaceAvailable?: boolean
  provisional?: boolean
  pinned?: boolean
}

export interface SidebarSection {
  family: SidebarSectionFamily
  label: string
  rows: SidebarSectionRow[]
}

/** Place query results into the three sidebar families; hide non-Web chat surfaces. */
function sidebarFamilyForSession(item: SessionItem): SidebarSectionFamily | null {
  if (item.sessionKind === 'chat') {
    if (['cli', 'tui', 'mcp', 'subagent'].includes(item.surface)) return null
    return 'chats'
  }
  if (item.sessionKind === 'task' || item.surface === 'subagent') return 'chats'
  if (item.sessionKind === 'channel') return 'channels'
  if (item.sessionKind === 'cron') return 'automations'
  return null
}

const SIDEBAR_SECTION_LABEL_KEYS: Record<SidebarSectionFamily, string> = {
  chats: 'sessions.filter.chats',
  channels: 'sessions.filter.channels',
  automations: 'sessions.filter.automations',
}

const SIDEBAR_SECTION_ORDER: SidebarSectionFamily[] = ['chats', 'channels', 'automations']

/** Build the ordered, recency-sorted sidebar projection without mutating query data. */
export function arrangeSidebarSections(
  items: SessionItem[],
  projects?: readonly ProjectWorkspaceItem[],
  sessionOrder: readonly string[] = [],
  pinnedSessionKeys: readonly string[] = [],
): SidebarSection[] {
  const buckets: Record<SidebarSectionFamily, SessionItem[]> = {
    chats: [],
    channels: [],
    automations: [],
  }
  for (const item of items) {
    if (!item.key || item.key === 'unknown') continue
    const family = sidebarFamilyForSession(item)
    if (!family) continue
    buckets[family].push(item)
  }

  const byRecency = (a: SessionItem, b: SessionItem) => (b.updatedAt || 0) - (a.updatedAt || 0)
  const orderIndex = new Map(sessionOrder.map((key, index) => [key, index]))
  const pinnedKeys = new Set(pinnedSessionKeys)
  const bySidebarOrder = (a: SessionItem, b: SessionItem) => {
    const pinnedDifference = Number(pinnedKeys.has(b.key)) - Number(pinnedKeys.has(a.key))
    if (pinnedDifference !== 0) return pinnedDifference
    const aIndex = orderIndex.get(a.key)
    const bIndex = orderIndex.get(b.key)
    if (aIndex !== undefined && bIndex !== undefined) return aIndex - bIndex
    // Sessions created after the last manual reorder remain at the top.
    if (aIndex !== undefined) return 1
    if (bIndex !== undefined) return -1
    return byRecency(a, b)
  }
  const toRow = (item: SessionItem, depth: number): SidebarSectionRow => ({
    rowKind: 'session',
    key: item.key,
    title: item.title,
    effectiveAgentId: item.effectiveAgentId,
    agentName: '',
    sessionKind: item.sessionKind,
    depth,
    runStatus: item.runStatus,
    runLabel: item.runLabel,
    taskAttention: ['queued', 'running'].includes(item.runStatus) ? 'running' : 'none',
    updatedAt: item.updatedAt || 0,
    hasContractGaps: item.hasContractGaps,
    workspace: item.workspace,
    workspaceId: item.workspaceId,
    workspaceLabel: item.workspaceLabel,
    workspaceDisplayPath: item.workspaceDisplayPath,
    provisional: item.provisional,
    pinned: pinnedKeys.has(item.key),
  })

  type WorkspaceBucket = {
    title: string
    displayPath?: string
    rows: SidebarSectionRow[]
    updatedAt: number
  }
  type WorkspaceTopLevel =
    | { kind: 'workspace'; workspace: string; index: number }
    | { kind: 'row'; row: SidebarSectionRow; index: number }

  const arrangeWorkspaceRows = (entries: SessionLedgerEntry[]): SidebarSectionRow[] => {
    const buckets = new Map<string, WorkspaceBucket>()
    const topLevel: WorkspaceTopLevel[] = []
    let index = 0

    for (const entry of entries) {
      const row = toRow(entry.item, entry.depth)
      const workspace = entry.item.workspace
      if (!workspace) {
        topLevel.push({ kind: 'row', row, index: index++ })
        continue
      }

      let bucket = buckets.get(workspace)
      if (!bucket) {
        bucket = {
          title: entry.item.workspaceLabel || workspace,
          displayPath: entry.item.workspaceDisplayPath || workspace,
          rows: [],
          updatedAt: 0,
        }
        buckets.set(workspace, bucket)
        topLevel.push({ kind: 'workspace', workspace, index: index++ })
      }
      bucket.rows.push({ ...row, depth: Math.min(row.depth + 1, 4) })
      bucket.updatedAt = Math.max(bucket.updatedAt, row.updatedAt || 0)
    }

    return [...topLevel]
      .sort((a, b) => a.index - b.index)
      .flatMap(entry => {
        if (entry.kind === 'row') return [entry.row]
        const bucket = buckets.get(entry.workspace)
        if (!bucket) return []
        const header: SidebarSectionRow = {
          rowKind: 'workspace',
          key: `workspace:${entry.workspace}`,
          title: bucket.title,
          effectiveAgentId: '',
          agentName: '',
          sessionKind: 'workspace',
          depth: 0,
          runStatus: 'idle',
          runLabel: '',
          taskAttention: 'none',
          updatedAt: bucket.updatedAt,
          hasContractGaps: false,
          workspace: entry.workspace,
          workspaceLabel: bucket.title,
          workspaceDisplayPath: bucket.displayPath || entry.workspace,
          workspaceTaskCount: bucket.rows.filter(row => row.depth === 1).length,
          workspacePinned: false,
          workspaceAvailable: true,
        }
        return [header, ...bucket.rows]
      })
  }

  const arrangePersistedProjectRows = (
    entries: SessionLedgerEntry[],
    persistedProjects: readonly ProjectWorkspaceItem[],
  ): SidebarSectionRow[] => {
    const rows: SidebarSectionRow[] = []
    for (const project of persistedProjects) {
      const projectEntries = entries.filter(entry => entry.item.workspaceId === project.id)
      rows.push({
        rowKind: 'workspace',
        key: `workspace:${project.id}`,
        title: project.name,
        effectiveAgentId: '',
        agentName: '',
        sessionKind: 'workspace',
        depth: 0,
        runStatus: 'idle',
        runLabel: '',
        taskAttention: 'none',
        updatedAt: 0,
        hasContractGaps: false,
        workspace: project.path,
        workspaceId: project.id,
        workspaceLabel: project.name,
        workspaceDisplayPath: project.path,
        workspaceTaskCount: project.taskCount
          + projectEntries.filter(entry => entry.item.provisional).length,
        workspacePinned: project.pinned,
        workspaceAvailable: project.available,
      })
      if (projectEntries.length === 0) {
        rows.push({
          rowKind: 'workspace-empty',
          key: `workspace:${project.id}:empty`,
          title: i18n.global.t('workspaces.noTasks'),
          effectiveAgentId: '',
          agentName: '',
          sessionKind: 'workspace-empty',
          depth: 1,
          runStatus: 'idle',
          runLabel: '',
          taskAttention: 'none',
          updatedAt: 0,
          hasContractGaps: false,
          workspace: project.path,
          workspaceId: project.id,
          workspaceLabel: project.name,
          workspaceDisplayPath: project.path,
        })
      } else {
        rows.push(...projectEntries.map(entry => ({
          ...toRow(entry.item, Math.min(entry.depth + 1, 4)),
          workspaceId: project.id,
        })))
      }
    }
    rows.push(
      ...entries
        .filter(entry => !entry.item.workspaceId)
        .map(entry => toRow(entry.item, entry.depth)),
    )
    // Sessions belonging to a removed project remain durable but hidden until
    // that project is restored to the canonical project list.
    return rows
  }

  return SIDEBAR_SECTION_ORDER.map(family => {
    const bucket = buckets[family]
    let rows: SidebarSectionRow[]
    if (family === 'chats') {
      const ledger = arrangeSessionLedger([...bucket].sort(bySidebarOrder))
      rows = projects === undefined
        ? arrangeWorkspaceRows(ledger)
        : arrangePersistedProjectRows(ledger, projects)
    } else {
      rows = [...bucket].sort(byRecency).map(item => toRow(item, 0))
    }
    return { family, label: i18n.global.t(SIDEBAR_SECTION_LABEL_KEYS[family]), rows }
  })
}

export function sessionMatches(item: SessionItem, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return [
    item.title,
    item.subtitle,
    item.groupLabel,
    item.effectiveAgentId,
    item.sessionKind,
    item.surface,
    item.conversationKind,
    item.status,
    item.runStatus,
    item.model,
    item.key,
  ].some(value => String(value || '').toLowerCase().includes(q))
}

export function groupSessions(items: SessionItem[]): SessionGroup[] {
  const groups = new Map<string, SessionGroup>()
  for (const item of items) {
    const label = item.groupLabel || i18n.global.t('sessions.group.contractGaps')
    const existing = groups.get(label)
    if (existing) {
      existing.items.push(item)
      existing.updatedAt = Math.max(existing.updatedAt, item.updatedAt || 0)
    } else {
      groups.set(label, {
        label,
        items: [item],
        updatedAt: item.updatedAt || 0,
      })
    }
  }
  return Array.from(groups.values())
    .map(group => ({
      ...group,
      items: [...group.items].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)),
    }))
    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
}

export function useSessions(directory: SessionDirectory) {
  const sessionsList = ref<SessionItem[]>([])
  const sessionListError = ref(false)
  const isLoading = ref(false)
  const isLoadingMore = ref(false)
  const loadMoreError = ref(false)
  const hasMore = ref(false)
  const nextCursor = ref<string | null>(null)
  let requestGeneration = 0
  let loadedPageCount = 0
  let pageCursors = new Set<string>()
  let activeRequest: AbortController | null = null

  function beginRequest() {
    activeRequest?.abort()
    activeRequest = new AbortController()
    return activeRequest
  }

  const allSessions = computed((): SessionItem[] =>
    [...sessionsList.value].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
  )

  const groupedSessions = computed((): SessionGroup[] => groupSessions(allSessions.value))

  function pageState(
    data: { hasMore: boolean; nextCursor: string | null },
    seenCursors: Set<string>,
    requestedCursor?: string,
  ) {
    const candidate = data.nextCursor
    // Missing metadata means an older server. A repeated cursor or a page that
    // claims more rows without a usable cursor is terminal, preventing loops.
    const usable = data.hasMore
      && candidate !== null
      && candidate !== requestedCursor
      && !seenCursors.has(candidate)
    if (usable && candidate) seenCursors.add(candidate)
    return {
      hasMore: usable,
      nextCursor: usable ? candidate : null,
    }
  }

  function appendUniqueSessions(
    target: SessionItem[],
    page: SessionItem[],
  ) {
    const seen = new Set(target.map(item => item.key))
    let added = 0
    for (const item of page) {
      const key = item.key
      if (!key || seen.has(key)) continue
      seen.add(key)
      target.push(item)
      added += 1
    }
    return added
  }

  async function loadSessions() {
    const generation = ++requestGeneration
    const request = beginRequest()
    const pagesToReload = Math.max(1, loadedPageCount)
    isLoading.value = true
    isLoadingMore.value = false
    sessionListError.value = false
    loadMoreError.value = false
    try {
      const refreshed: SessionItem[] = []
      const refreshedCursors = new Set<string>()
      let requestedCursor: string | undefined
      let refreshedPageCount = 0
      let refreshedPageState = { hasMore: false, nextCursor: null as string | null }

      while (refreshedPageCount < pagesToReload) {
        const data = await directory.listPage({
          limit: SESSION_LIST_PAGE_SIZE,
          cursor: requestedCursor,
          signal: request.signal,
        })
        if (generation !== requestGeneration) return
        const added = appendUniqueSessions(refreshed, data.items)
        refreshedPageCount += 1
        refreshedPageState = pageState(data, refreshedCursors, requestedCursor)
        if (added === 0) {
          refreshedPageState = { hasMore: false, nextCursor: null }
          break
        }
        if (!refreshedPageState.hasMore || !refreshedPageState.nextCursor) break
        requestedCursor = refreshedPageState.nextCursor
      }

      if (generation !== requestGeneration) return
      sessionsList.value = refreshed
      loadedPageCount = refreshedPageCount
      pageCursors = refreshedCursors
      hasMore.value = refreshedPageState.hasMore
      nextCursor.value = refreshedPageState.nextCursor
    } catch (err: unknown) {
      if (generation !== requestGeneration) return
      console.error('[useSessions] session directory error:', err instanceof Error ? err.message : err)
      // A reconnect/event refresh must not collapse an already useful ledger.
      // Keep the last complete traversal and its retry cursor until a whole
      // replacement snapshot succeeds.
      if (sessionsList.value.length === 0) {
        sessionListError.value = true
        hasMore.value = false
        nextCursor.value = null
        loadedPageCount = 0
        pageCursors = new Set<string>()
      }
    } finally {
      if (generation === requestGeneration) isLoading.value = false
    }
  }

  async function loadMoreSessions() {
    const cursor = nextCursor.value
    if (!hasMore.value || !cursor || isLoading.value || isLoadingMore.value) return
    const generation = requestGeneration
    const request = beginRequest()
    isLoadingMore.value = true
    loadMoreError.value = false
    try {
      const data = await directory.listPage({
        limit: SESSION_LIST_PAGE_SIZE,
        cursor,
        signal: request.signal,
      })
      if (generation !== requestGeneration || nextCursor.value !== cursor) return
      const appended = [...sessionsList.value]
      const added = appendUniqueSessions(appended, data.items)
      sessionsList.value = appended
      loadedPageCount += 1
      const nextPageState = pageState(data, pageCursors, cursor)
      hasMore.value = added > 0 && nextPageState.hasMore
      nextCursor.value = hasMore.value ? nextPageState.nextCursor : null
    } catch (err: unknown) {
      if (generation !== requestGeneration) return
      console.error('[useSessions] session directory next-page error:', err instanceof Error ? err.message : err)
      loadMoreError.value = true
    } finally {
      if (generation === requestGeneration) isLoadingMore.value = false
    }
  }

  return {
    sessionsList,
    sessionListError,
    isLoading,
    isLoadingMore,
    loadMoreError,
    hasMore,
    groupedSessions,
    allSessions,
    loadSessions,
    loadMoreSessions,
  }
}
