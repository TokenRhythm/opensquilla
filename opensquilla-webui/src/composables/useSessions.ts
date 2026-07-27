import { ref, computed } from 'vue'
import i18n from '@/i18n'
import { useRpcStore } from '@/stores/rpc'
import type { RawSessionItem, RawSessionListEntry, SessionsListResponse } from '@/types/rpc'

export const SESSION_LIST_VIEW = 'session-list-v1'

export type { RawSessionItem } from '@/types/rpc'

export interface SessionItem {
  key: string
  title: string
  subtitle: string
  groupLabel: string
  workspace?: string
  workspaceLabel?: string
  workspaceDisplayPath?: string
  effectiveAgentId: string
  sessionKind: string
  surface: string
  conversationKind: string
  threadLabel: string
  channelContext: { name?: string; id?: string; accountId?: string; threadId?: string } | null
  status: string
  visualStatus: string
  runStatus: string
  runLabel: string
  messageCount: number | null
  updatedAt: number
  interactive: boolean
  /** True when this session was forked from its parent's transcript. */
  forkedFromParent: boolean
  contractGaps: string[]
  raw: RawSessionItem
}

export interface SessionGroup {
  label: string
  items: SessionItem[]
  updatedAt: number
}

function hasOwn(obj: unknown, field: string): boolean {
  return !!obj && Object.prototype.hasOwnProperty.call(obj, field)
}

export function itemKey(item: unknown): string {
  if (typeof item === 'string') return item
  const row = item && typeof item === 'object' ? item as RawSessionItem : null
  return row?.key || row?.session || row?.sessionKey || ''
}

function textValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function numberValue(value: unknown): number | null {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

export function normalizeRunStatus(status: string | undefined): string {
  const value = String(status || '').toLowerCase()
  if (value === 'abandoned') return 'interrupted'
  if (value === 'killed') return 'cancelled'
  if (['succeeded', 'success', 'complete'].includes(value)) return 'idle'
  if (['queued', 'running', 'interrupted', 'failed', 'timeout', 'cancelled', 'idle'].includes(value)) return value
  return 'idle'
}

function stoppedTaskDurationSeconds(row: RawSessionItem | undefined): number | null {
  const lastTask = row?.last_task || row?.lastTask || null
  if (!lastTask) return null
  const startedAt = numberValue(lastTask.started_at) ?? numberValue(lastTask.startedAt)
  const finishedAt = numberValue(lastTask.finished_at) ?? numberValue(lastTask.finishedAt)
  if (startedAt == null || finishedAt == null || finishedAt < startedAt) return null
  return Math.max(1, Math.round((finishedAt - startedAt) / 1000))
}

export function runStatusLabelText(status: string, row?: RawSessionItem): string {
  const t = i18n.global.t
  if (status === 'cancelled' || status === 'interrupted') {
    const seconds = stoppedTaskDurationSeconds(row)
    if (seconds != null) return t('sessions.status.stoppedAfterSeconds', { seconds })
    return t('sessions.status.outputInterrupted')
  }
  const keys: Record<string, string> = {
    queued: 'sessions.status.queued',
    running: 'sessions.status.running',
    interrupted: 'sessions.status.interrupted',
    failed: 'sessions.status.failed',
    timeout: 'sessions.status.timeout',
    cancelled: 'sessions.status.cancelled',
    idle: 'sessions.status.idle',
  }
  return t(keys[status] || 'sessions.status.idle')
}

function terminalRunStatus(row: RawSessionItem): string {
  const lastTask = row.last_task || row.lastTask || null
  const rawStatus = lastTask?.status || row.terminal_status || row.terminalStatus || ''
  const status = normalizeRunStatus(rawStatus)
  return ['failed', 'timeout', 'cancelled', 'interrupted'].includes(status) ? status : ''
}

export function sessionRunStatus(row: RawSessionItem): string {
  const active = row.active_task || row.activeTask || null
  const activeStatus = active ? normalizeRunStatus(active.status) : ''
  const terminal = terminalRunStatus(row)
  const rawStatus = row.runStatus || row.run_status || active?.status || terminal || ''
  const runStatus = normalizeRunStatus(rawStatus)
  if (active && (activeStatus === 'queued' || activeStatus === 'running')) return activeStatus
  if (terminal) return normalizeRunStatus(terminal)
  return runStatus
}

export function sessionVisualStatus(row: Pick<SessionItem, 'status' | 'runStatus'>): string {
  if (row.runStatus === 'failed' || row.runStatus === 'timeout') return row.runStatus
  if (row.runStatus === 'cancelled' || row.runStatus === 'interrupted') return 'killed'
  if (row.runStatus === 'queued' || row.runStatus === 'running' || row.runStatus === 'idle') return row.runStatus
  return String(row.status || 'unknown').toLowerCase()
}

function fallbackTitle(row: RawSessionItem): string {
  return (
    textValue(row.display_name) ||
    textValue(row.displayName) ||
    textValue(row.subject) ||
    textValue(row.derived_title) ||
    textValue(row.derivedTitle) ||
    ''
  )
}

function keyAgentId(key: string): string {
  const parts = key.split(':')
  return parts[0] === 'agent' && parts[1] ? parts[1] : ''
}

function sourceKind(row: RawSessionItem): string {
  return textValue(row.sourceKind) || textValue(row.source_kind)
}

function channelKind(row: RawSessionItem): string {
  return textValue(row.channelKind) || textValue(row.channel_kind) || textValue(row.lastChannel) || textValue(row.last_channel)
}

function deriveConversationKind(row: RawSessionItem, key: string): string {
  const explicit = textValue(row.conversationKind)
  if (explicit) return explicit
  const source = sourceKind(row).toLowerCase()
  const channel = channelKind(row).toLowerCase()
  const chatType = (textValue(row.chatType) || textValue(row.chat_type)).toLowerCase()
  if (key.includes(':webchat:') || chatType === 'webchat' || source === 'webui' || channel === 'webchat') return 'direct'
  if (key.startsWith('cron:') || key.includes(':cron:') || source === 'cron' || channel === 'cron') return 'unknown'
  if (source === 'channel' || (!!channel && !['cli', 'subagent', 'standalone'].includes(channel))) return chatType || 'group'
  if (key.includes(':subagent:') || source === 'subagent' || channel === 'subagent') return 'internal'
  if (key.includes(':cli:') || key.includes(':standalone:') || source === 'cli' || channel === 'cli') return 'internal'
  return 'unknown'
}

function deriveSessionKind(row: RawSessionItem, key: string): string {
  const explicit = textValue(row.sessionKind)
  if (explicit) return explicit
  const source = sourceKind(row).toLowerCase()
  const channel = channelKind(row).toLowerCase()
  if (key.includes(':webchat:') || source === 'webui' || channel === 'webchat') return 'chat'
  if (key.startsWith('cron:') || key.includes(':cron:') || source === 'cron' || channel === 'cron') return 'cron'
  if (source === 'channel' || (!!channel && !['cli', 'subagent', 'standalone'].includes(channel))) return 'channel'
  if (key.includes(':subagent:') || source === 'subagent' || channel === 'subagent') return 'task'
  if (key.includes(':cli:') || key.includes(':standalone:') || source === 'cli' || channel === 'cli') return 'chat'
  return 'unknown'
}

function deriveSurface(row: RawSessionItem, key: string, sessionKind: string): string {
  const explicit = textValue(row.surface)
  if (explicit) return explicit
  const channel = channelKind(row)
  const source = sourceKind(row)
  if (sessionKind === 'chat' && key.includes(':webchat:')) return 'webchat'
  if (sessionKind === 'cron') return channel || source || 'cron'
  if (sessionKind === 'channel') return channel || source || 'channel'
  if (key.includes(':subagent:')) return 'subagent'
  if (key.includes(':cli:') || key.includes(':standalone:')) return 'cli'
  return source || channel || 'unknown'
}

function deliveryContextValue(row: RawSessionItem, field: string): string {
  const ctx = row.deliveryContext || row.delivery_context || null
  return ctx && typeof ctx === 'object' ? textValue(ctx[field]) : ''
}

function deriveGroupLabel(row: RawSessionItem, key: string, sessionKind: string, agentId: string): string {
  const explicit = textValue(row.groupLabel)
  if (explicit) return explicit
  if (sessionKind === 'chat') return agentId || keyAgentId(key) || 'main'
  if (sessionKind === 'cron') {
    return (
      textValue(row.cron?.name) ||
      textValue(row.cron?.jobId) ||
      textValue(row.cron?.id) ||
      textValue(row.subject) ||
      i18n.global.t('sessions.kindLabel.cron')
    )
  }
  if (sessionKind === 'channel') {
    const channel = channelKind(row) || i18n.global.t('sessions.kindLabel.channel')
    const target = (
      textValue(row.lastTo) ||
      textValue(row.last_to) ||
      textValue(row.channelId) ||
      textValue(row.channel_id) ||
      deliveryContextValue(row, 'channel_id') ||
      deliveryContextValue(row, 'thread_id') ||
      textValue(row.groupId) ||
      textValue(row.group_id)
    )
    return target ? `${channel} / ${target}` : channel
  }
  return i18n.global.t('sessions.group.operational')
}

function fallbackSessionTitle(row: RawSessionItem, key: string, sessionKind: string): string {
  const t = i18n.global.t
  const semantic = fallbackTitle(row)
  if (semantic) return semantic
  if (sessionKind === 'chat') return t('sessions.fallbackTitle.chat')
  if (sessionKind === 'cron') return textValue(row.subject) || t('sessions.fallbackTitle.cron')
  if (sessionKind === 'channel') return textValue(row.subject) || t('sessions.fallbackTitle.channel')
  if (sessionKind === 'task') return t('sessions.fallbackTitle.task')
  return key || t('sessions.fallbackTitle.untitled')
}

function normalizeUpdatedAt(row: RawSessionItem, gaps: string[]): number {
  const activityValue = numberValue(row.lastActivityAt) ?? numberValue(row.last_activity_at)
  if (activityValue != null) return activityValue
  const contractValue = numberValue(row.updatedAt)
  if (contractValue != null) return contractValue
  gaps.push('updatedAt')
  return numberValue(row.updated_at) || 0
}

function normalizeMessageCount(row: RawSessionItem, gaps: string[]): number | null {
  const contractValue = numberValue(row.messageCount)
  if (contractValue != null) return contractValue
  gaps.push('messageCount')
  return numberValue(row.message_count) ?? numberValue(row.entry_count)
}

function normalizeEffectiveAgentId(row: RawSessionItem, gaps: string[], fallback = 'unknown'): string {
  const effective = textValue(row.effectiveAgentId)
  if (effective) return effective
  gaps.push('effectiveAgentId')
  return textValue(row.agentId) || textValue(row.agent_id) || fallback
}

function normalizeRequiredString(
  row: RawSessionItem,
  field: keyof RawSessionItem,
  fallback: string,
  gaps: string[],
): string {
  const value = textValue(row[field])
  if (value) return value
  gaps.push(String(field))
  return fallback
}

export function normalizeSessionItem(item: unknown): SessionItem | null {
  const raw: RawSessionItem = typeof item === 'string' ? { key: item } : (item || {}) as RawSessionItem
  const key = itemKey(item)
  if (!key || key === 'unknown') return null

  const gaps: string[] = []
  const derivedAgentId = textValue(raw.effectiveAgentId) || textValue(raw.agentId) || textValue(raw.agent_id) || keyAgentId(key) || 'unknown'
  const sessionKind = deriveSessionKind(raw, key)
  const conversationKind = deriveConversationKind(raw, key)
  const surface = deriveSurface(raw, key, sessionKind)
  const groupLabel = deriveGroupLabel(raw, key, sessionKind, derivedAgentId)
  const workspace = textValue(raw.workspace)
  const workspaceLabel = textValue(raw.workspaceLabel)
  const workspaceDisplayPath = textValue(raw.workspaceDisplayPath)
  let title = normalizeRequiredString(raw, 'title', fallbackSessionTitle(raw, key, sessionKind), gaps)
  if (sessionKind === 'task' && /^you are a subagent\b/i.test(title)) title = i18n.global.t('sessions.fallbackTitle.task')
  const subtitle = hasOwn(raw, 'subtitle') ? textValue(raw.subtitle) : ''
  if (!hasOwn(raw, 'subtitle')) gaps.push('subtitle')
  const effectiveAgentId = normalizeEffectiveAgentId(raw, gaps, derivedAgentId)
  if (!hasOwn(raw, 'sessionKind')) gaps.push('sessionKind')
  if (!hasOwn(raw, 'surface')) gaps.push('surface')
  if (!hasOwn(raw, 'conversationKind')) gaps.push('conversationKind')
  const messageCount = normalizeMessageCount(raw, gaps)
  const updatedAt = normalizeUpdatedAt(raw, gaps)
  const status = textValue(raw.status) || 'unknown'
  if (!hasOwn(raw, 'runStatus')) gaps.push('runStatus')
  const runStatus = sessionRunStatus(raw)
  const thread = raw.thread && typeof raw.thread === 'object' ? raw.thread : null
  const threadLabel = thread?.kind && thread?.id ? `${thread.kind} ${thread.id}` : ''
  const channelContext = raw.channelContext && typeof raw.channelContext === 'object' ? raw.channelContext : null

  return {
    key,
    title,
    subtitle,
    groupLabel,
    workspace: workspace || undefined,
    workspaceLabel: workspaceLabel || undefined,
    workspaceDisplayPath: workspaceDisplayPath || undefined,
    effectiveAgentId,
    sessionKind,
    surface,
    conversationKind,
    threadLabel,
    channelContext,
    status,
    visualStatus: sessionVisualStatus({ status, runStatus }),
    runStatus,
    runLabel: runStatusLabelText(runStatus, raw),
    messageCount,
    updatedAt,
    interactive: raw.interactive === true,
    forkedFromParent: raw.forkedFromParent === true || raw.forked_from_parent === true,
    contractGaps: Array.from(new Set(gaps)),
    raw,
  }
}

function parentField(item: SessionItem): Record<string, unknown> | null {
  const parent = item.raw.parent
  return parent && typeof parent === 'object' ? parent as Record<string, unknown> : null
}

/** Parent session key for subagent rows, when the contract carries one. */
export function sessionParentKey(item: SessionItem): string {
  const key = parentField(item)?.key
  return typeof key === 'string' ? key.trim() : ''
}

/** Spawn depth from the session contract; 0 when the row is not a subagent. */
export function sessionSpawnDepth(item: SessionItem): number {
  const raw = parentField(item)?.spawnDepth
  const depth = Number(raw)
  return Number.isFinite(depth) && depth > 0 ? depth : 0
}

/** Parent title carried by the contract, used to label subagent lineage. */
export function sessionParentTitle(item: SessionItem): string {
  const title = parentField(item)?.title
  return typeof title === 'string' ? title.trim() : ''
}

export interface SessionLedgerEntry {
  item: SessionItem
  depth: number
  /** Resolved parent title for subagent rows; empty for root rows. */
  parentTitle: string
}

/**
 * Flatten sessions into ledger order: rows keep their recency sort, and
 * subagent rows indent directly under their parent when the parent is in
 * the same list. Orphan subagents stay at the root level.
 */
export function arrangeSessionLedger(items: SessionItem[]): SessionLedgerEntry[] {
  const byKey = new Map(items.map(item => [item.key, item]))
  const children = new Map<string, SessionItem[]>()
  const roots: SessionItem[] = []
  for (const item of items) {
    const parentKey = sessionParentKey(item)
    if (parentKey && parentKey !== item.key && byKey.has(parentKey)) {
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
    // the contract marks it spawned; its lineage label falls back to the
    // parent title carried on the contract.
    const orphanDepth = sessionSpawnDepth(root) > 0 ? 1 : 0
    visit(root, orphanDepth, orphanDepth > 0 ? sessionParentTitle(root) : '')
  }
  return entries
}

/** Family buckets rendered as collapsible sidebar sections, in display order. */
export type SidebarSectionFamily = 'chats' | 'channels' | 'automations'

/** A single rendered sidebar row, flattened with its indent depth. */
export interface SidebarSectionRow {
  rowKind: 'session' | 'workspace'
  key: string
  title: string
  effectiveAgentId: string
  /** Resolved display name when known; empty when the caller must resolve it. */
  agentName: string
  sessionKind: string
  /** Indent level mirroring `arrangeSessionLedger` (0 = root, subagents > 0). */
  depth: number
  runStatus: string
  runLabel: string
  updatedAt: number
  hasContractGaps: boolean
  workspace?: string
  workspaceLabel?: string
  workspaceDisplayPath?: string
}

/** One collapsible family section with its recency-ordered rows. */
export interface SidebarSection {
  family: SidebarSectionFamily
  label: string
  rows: SidebarSectionRow[]
}

/**
 * Decide which sidebar family a session belongs to, mirroring App.vue's
 * `sourceFamilyForSession`. Unlike the old flat list, subagent rows are no
 * longer dropped: a 'task'/'subagent' session is folded into the Chats family
 * (nested under its parent by `arrangeSessionLedger`), so this returns 'chats'
 * for it. Returns null for sessions that have no sidebar home (e.g. cli/tui/mcp
 * chat surfaces, or unknown kinds).
 */
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

/**
 * Arrange sessions into the ordered sidebar families (Chats, Channels,
 * Automations). Each family is recency-sorted; the Chats family additionally
 * runs through `arrangeSessionLedger`, so subagent rows indent under their
 * parent chat (and orphan subagents indent at depth 1 via the contract's
 * spawn-depth fallback). The helper is pure: it returns all three families
 * (callers drop empty ones at render time).
 */
export function arrangeSidebarSections(items: SessionItem[]): SidebarSection[] {
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
    updatedAt: item.updatedAt || 0,
    hasContractGaps: item.contractGaps.length > 0,
    workspace: item.workspace,
    workspaceLabel: item.workspaceLabel,
    workspaceDisplayPath: item.workspaceDisplayPath,
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

    const topLevelUpdatedAt = (entry: WorkspaceTopLevel): number => {
      if (entry.kind === 'row') return entry.row.updatedAt || 0
      return buckets.get(entry.workspace)?.updatedAt || 0
    }

    return [...topLevel]
      .sort((a, b) => topLevelUpdatedAt(b) - topLevelUpdatedAt(a) || a.index - b.index)
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
          updatedAt: bucket.updatedAt,
          hasContractGaps: false,
          workspace: entry.workspace,
          workspaceLabel: bucket.title,
          workspaceDisplayPath: bucket.displayPath || entry.workspace,
        }
        return [header, ...bucket.rows]
      })
  }

  return SIDEBAR_SECTION_ORDER.map(family => {
    const bucket = buckets[family]
    let rows: SidebarSectionRow[]
    if (family === 'chats') {
      // Recency-sort first so the ledger's root ordering follows recency, then
      // flatten parent → child so subagents indent directly beneath their chat.
      const ledger = arrangeSessionLedger([...bucket].sort(byRecency))
      rows = arrangeWorkspaceRows(ledger)
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
    item.raw.model,
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

export function useSessions() {
  const rpc = useRpcStore()
  const sessionsList = ref<RawSessionListEntry[]>([])
  const sessionListError = ref(false)
  const isLoading = ref(false)

  const allSessions = computed((): SessionItem[] =>
    sessionsList.value
      .map(normalizeSessionItem)
      .filter((item): item is SessionItem => !!item)
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
  )

  const groupedSessions = computed((): SessionGroup[] => groupSessions(allSessions.value))

  async function loadSessions() {
    isLoading.value = true
    sessionListError.value = false
    try {
      await rpc.waitForConnection()
      const data = await rpc.call<SessionsListResponse>('sessions.list', { limit: 200, view: SESSION_LIST_VIEW })
      const raw = data?.sessions || data?.keys || []
      sessionsList.value = raw.filter(s => !!itemKey(s))
    } catch (err: unknown) {
      console.error('[useSessions] sessions.list error:', err instanceof Error ? err.message : err)
      sessionListError.value = true
    } finally {
      isLoading.value = false
    }
  }

  return {
    sessionsList,
    sessionListError,
    isLoading,
    groupedSessions,
    allSessions,
    loadSessions,
  }
}
