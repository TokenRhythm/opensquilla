<script lang="ts">
import type { SidebarSection, SidebarSectionFamily, SidebarSectionRow } from '@/composables/useSessions'

export type { SidebarSection, SidebarSectionFamily, SidebarSectionRow } from '@/composables/useSessions'

/** Legacy family id kept for the agent-initial filter callers. */
export type SidebarFamilyId = SidebarSectionFamily

/**
 * A rendered sidebar row: the pure `SidebarSectionRow` produced by
 * `arrangeSidebarSections`, with `agentName` resolved by App.vue (the composable
 * leaves it empty so the display-name lookup stays in one place).
 */
export type SidebarConversationItem = SidebarSectionRow

const COLLAPSE_STORAGE_KEY = 'opensquilla-sidebar-sections'

export function readSidebarCollapsedState(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(COLLAPSE_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed as Record<string, boolean> : {}
  } catch {
    return {}
  }
}

function writeSidebarCollapsedState(state: Record<string, boolean>) {
  try {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
}

export { COLLAPSE_STORAGE_KEY, writeSidebarCollapsedState }
export type { SidebarSection as SidebarSectionType }
</script>

<script setup lang="ts">
import { computed, nextTick, ref, watch, type ComponentPublicInstance } from 'vue'
import { useI18n } from 'vue-i18n'
import type { SessionTaskAttention } from '@/composables/useSessionTaskAttention'
import Icon from './Icon.vue'
import { useConfirm } from '@/composables/useConfirm'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { shouldShowAgentFilterBadge } from '@/utils/sidebarConversations'

const props = withDefaults(defineProps<{
  sections: SidebarSection[]
  error: boolean
  loading: boolean
  currentKey: string
  contractDebugEnabled: boolean
  /** Command-palette chord, shown in the search button's tooltip. */
  searchHint: string
  canManageProjects?: boolean
  canCreateProjects?: boolean
}>(), {
  canManageProjects: false,
  canCreateProjects: false,
})

const emit = defineEmits<{
  (e: 'select', key: string): void
  (e: 'refresh'): void
  (e: 'rename', payload: { key: string; title: string }): void
  (e: 'delete', key: string): void
  (e: 'bulk-delete', keys: string[]): void
  (e: 'reorder', payload: { draggedKey: string; targetKey: string; position: 'before' | 'after' }): void
  (e: 'session-pin', payload: { key: string; pinned: boolean }): void
  (e: 'new-chat'): void
  (e: 'new-project'): void
  (e: 'new-project-task', workspaceId: string): void
  (e: 'project-pin', payload: { workspaceId: string; pinned: boolean }): void
  (e: 'project-edit', workspaceId: string): void
  (e: 'project-delete-history', workspaceId: string): void
  (e: 'project-remove', workspaceId: string): void
  (e: 'search'): void
}>()

const { confirm } = useConfirm()
const { t } = useI18n()

const TASK_ATTENTION_LABEL_KEYS: Record<Exclude<SessionTaskAttention, 'none'>, string> = {
  running: 'shared.sidebar.taskRunning',
  completed: 'shared.sidebar.taskCompletedUnread',
  failed: 'shared.sidebar.taskUnfinishedUnread',
}

function taskAttentionLabel(attention: SessionTaskAttention | undefined): string {
  if (!attention || attention === 'none') return ''
  const key = TASK_ATTENTION_LABEL_KEYS[attention]
  return key ? t(key) : ''
}

/* ── Agent filter (lives within the Chats section) ─────────────────── */

const agentFilter = ref('')

function toggleAgentFilter(agentId: string) {
  agentFilter.value = agentFilter.value === agentId ? '' : agentId
}

function clearAgentFilter() {
  agentFilter.value = ''
}

const agentFilterName = computed(() => {
  if (!agentFilter.value) return ''
  for (const section of props.sections) {
    const match = section.rows.find(row => row.effectiveAgentId === agentFilter.value)
    if (match) return match.agentName || agentFilter.value
  }
  return agentFilter.value
})

function agentInitial(name: string): string {
  return name.trim().charAt(0).toUpperCase() || '?'
}

function isWorkspaceRow(row: SidebarConversationItem): boolean {
  return row.rowKind === 'workspace'
}

function filterChatRowsByAgent(rows: SidebarConversationItem[], agentId: string): SidebarConversationItem[] {
  const result: SidebarConversationItem[] = []
  let pendingWorkspace: SidebarConversationItem | null = null
  let pendingWorkspaceHasMatch = false

  const flushPendingWorkspace = () => {
    if (pendingWorkspace && pendingWorkspaceHasMatch) result.push(pendingWorkspace)
    pendingWorkspace = null
    pendingWorkspaceHasMatch = false
  }

  for (const row of rows) {
    if (isWorkspaceRow(row)) {
      flushPendingWorkspace()
      pendingWorkspace = row
      continue
    }
    if (row.effectiveAgentId !== agentId) continue
    if (pendingWorkspace && !pendingWorkspaceHasMatch) {
      result.push(pendingWorkspace)
      pendingWorkspaceHasMatch = true
    }
    result.push(row)
  }
  flushPendingWorkspace()
  return result
}

/* ── Collapsible sections ──────────────────────────────────────────── */

// Persisted collapse state, keyed by family. A family is open unless an
// explicit `true` (collapsed) flag was stored for it; Chats opens by default.
const collapsed = ref<Record<string, boolean>>(readSidebarCollapsedState())

function isCollapsed(family: SidebarFamilyId): boolean {
  return collapsed.value[family] === true
}

function toggleSection(family: SidebarFamilyId) {
  const next = { ...collapsed.value, [family]: !isCollapsed(family) }
  collapsed.value = next
  writeSidebarCollapsedState(next)
}

function projectCollapseKey(workspaceId: string): string {
  return `project:${workspaceId}`
}

function isProjectCollapsed(row: SidebarConversationItem): boolean {
  return Boolean(row.workspaceId && collapsed.value[projectCollapseKey(row.workspaceId)] === true)
}

function toggleProject(row: SidebarConversationItem) {
  if (!row.workspaceId) return
  const key = projectCollapseKey(row.workspaceId)
  const next = { ...collapsed.value, [key]: !isProjectCollapsed(row) }
  collapsed.value = next
  writeSidebarCollapsedState(next)
}

function startProjectTask(row: SidebarConversationItem) {
  if (!row.workspaceId || row.workspaceAvailable === false) return
  const key = projectCollapseKey(row.workspaceId)
  const next = { ...collapsed.value, [key]: false }
  collapsed.value = next
  writeSidebarCollapsedState(next)
  emit('new-project-task', row.workspaceId)
}

function filterCollapsedProjectRows(rows: SidebarConversationItem[]): SidebarConversationItem[] {
  const hiddenProjects = new Set<string>()
  const result: SidebarConversationItem[] = []
  for (const row of rows) {
    if (row.rowKind === 'workspace') {
      if (row.workspaceId && isProjectCollapsed(row)) hiddenProjects.add(row.workspaceId)
      result.push(row)
      continue
    }
    if (row.workspaceId && hiddenProjects.has(row.workspaceId)) continue
    result.push(row)
  }
  return result
}

// Sections with at least one row, honoring the agent filter inside Chats.
const visibleSections = computed(() => {
  return props.sections
    .map(section => {
      const filteredRows = section.family === 'chats' && agentFilter.value
        ? filterChatRowsByAgent(section.rows, agentFilter.value)
        : section.rows
      return {
        ...section,
        rows: filterCollapsedProjectRows(filteredRows)
          .filter(row => row.rowKind !== 'workspace-empty'),
      }
    })
    .filter(section => section.rows.length > 0)
})

const visibleProjectCount = computed(() =>
  visibleSections.value.reduce(
    (sum, section) => sum + section.rows.filter(row => row.rowKind === 'workspace').length,
    0,
  ),
)

/**
 * `arrangeSidebarSections` emits project rows as a small ordered ledger:
 * a workspace header followed by its task rows. Track those task keys without
 * treating every session cwd as a project — ordinary tasks can still carry a
 * workspace path even when they are not bound to a persisted project.
 */
const projectRowKeys = computed(() => {
  const keys = new Set<string>()
  const chats = props.sections.find(section => section.family === 'chats')
  let project: SidebarConversationItem | null = null

  for (const row of chats?.rows || []) {
    if (row.rowKind === 'workspace') {
      project = row
      keys.add(row.key)
      continue
    }
    if (row.rowKind === 'workspace-empty') {
      if (project) keys.add(row.key)
      continue
    }
    const belongsToProject = Boolean(
      project
      && (
        project.workspaceId
          ? row.workspaceId === project.workspaceId
          : Boolean(project.workspace && row.workspace === project.workspace)
      ),
    )
    if (belongsToProject) keys.add(row.key)
    else project = null
  }
  return keys
})

const firstRecentRowKey = computed(() => {
  for (const section of visibleSections.value) {
    const row = section.rows.find(item =>
      item.rowKind === 'session' && !projectRowKeys.value.has(item.key),
    )
    if (row) return row.key
  }
  return ''
})

const hasVisibleRecentRows = computed(() =>
  visibleSections.value.some(section =>
    section.rows.some(row =>
      row.rowKind === 'session' && !projectRowKeys.value.has(row.key),
    ),
  ),
)

// Total rendered rows: drives the onboarding empty-state and the filter's
// "No matches" message separately from a true first-run empty list.
const totalRows = computed(() =>
  props.sections.reduce(
    (sum, section) => sum + section.rows.filter(row => row.rowKind === 'session').length,
    0,
  ),
)

const hasFilterMatches = computed(() =>
  visibleSections.value.some(section => section.rows.some(row => !isWorkspaceRow(row))),
)

/* ── Session drag ordering ────────────────────────────────────────── */

const draggedRowKey = ref('')
const draggedRowScope = ref('')
const dropTargetKey = ref('')
const dropPosition = ref<'before' | 'after'>('before')
const pointerDrag = ref<{
  key: string
  scope: string
  startX: number
  startY: number
  active: boolean
} | null>(null)
const suppressSelectKey = ref('')

function reorderScope(row: SidebarConversationItem): string {
  const pinGroup = row.pinned ? 'pinned' : 'regular'
  if (!projectRowKeys.value.has(row.key)) return `recents:${pinGroup}`
  return `project:${row.workspaceId || row.workspace || ''}:${pinGroup}`
}

function canDragRow(row: SidebarConversationItem): boolean {
  return row.rowKind === 'session'
    && row.sessionKind === 'chat'
    && !row.provisional
    && !selectionMode.value
    && !agentFilter.value
    && renamingKey.value !== row.key
}

function clearRowDrag() {
  draggedRowKey.value = ''
  draggedRowScope.value = ''
  dropTargetKey.value = ''
  pointerDrag.value = null
}

function findSessionRow(key: string): SidebarConversationItem | undefined {
  for (const section of props.sections) {
    const row = section.rows.find(candidate => candidate.key === key)
    if (row) return row
  }
  return undefined
}

function onRowPointerDown(row: SidebarConversationItem, event: PointerEvent) {
  if (event.button !== 0 || !canDragRow(row)) return
  const target = event.target
  if (target instanceof Element && target.closest('.sidebar-row-menu-wrap, input, .sidebar-agent-badge')) return
  pointerDrag.value = {
    key: row.key,
    scope: reorderScope(row),
    startX: event.clientX,
    startY: event.clientY,
    active: false,
  }
}

useDocumentEvent('pointermove', (event) => {
  const drag = pointerDrag.value
  if (!drag) return
  if (!drag.active) {
    if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 6) return
    drag.active = true
    draggedRowKey.value = drag.key
    draggedRowScope.value = drag.scope
  }
  event.preventDefault()
  const target = document.elementFromPoint(event.clientX, event.clientY)
    ?.closest<HTMLElement>('.sidebar-history-row[data-session-key]')
  const targetKey = target?.dataset.sessionKey || ''
  const row = findSessionRow(targetKey)
  if (!target || !row || row.key === drag.key || !canDragRow(row) || reorderScope(row) !== drag.scope) {
    dropTargetKey.value = ''
    return
  }
  const rect = target.getBoundingClientRect()
  dropTargetKey.value = row.key
  dropPosition.value = event.clientY < rect.top + rect.height / 2 ? 'before' : 'after'
}, { passive: false })

useDocumentEvent('pointerup', () => {
  const drag = pointerDrag.value
  if (!drag) return
  if (drag.active) {
    suppressSelectKey.value = drag.key
    if (dropTargetKey.value) {
      emit('reorder', {
        draggedKey: drag.key,
        targetKey: dropTargetKey.value,
        position: dropPosition.value,
      })
    }
  }
  clearRowDrag()
})

useDocumentEvent('pointercancel', clearRowDrag)

/* ── Bulk selection ───────────────────────────────────────────────── */

const selectedKeys = ref<Set<string>>(new Set())
const selectionMode = ref(false)

const visibleSelectableRows = computed(() =>
  visibleSections.value.flatMap(section =>
    isCollapsed(section.family)
      ? []
      : section.rows.filter(row => row.rowKind === 'session' && !row.provisional),
  ),
)

const visibleSelectableKeySet = computed(() =>
  new Set(visibleSelectableRows.value.map(row => row.key)),
)

const selectedCount = computed(() => selectedKeys.value.size)
const visibleSelectableCount = computed(() => visibleSelectableRows.value.length)

const allVisibleSelected = computed(() =>
  visibleSelectableCount.value > 0
  && visibleSelectableRows.value.every(row => selectedKeys.value.has(row.key)),
)

watch(visibleSelectableKeySet, (keys) => {
  const next = new Set([...selectedKeys.value].filter(key => keys.has(key)))
  if (next.size !== selectedKeys.value.size) selectedKeys.value = next
})

function isRowSelected(key: string): boolean {
  return selectedKeys.value.has(key)
}

function setRowSelected(key: string, checked: boolean) {
  const next = new Set(selectedKeys.value)
  if (checked) next.add(key)
  else next.delete(key)
  selectedKeys.value = next
}

function toggleVisibleSelection() {
  const checked = !allVisibleSelected.value
  const next = new Set(selectedKeys.value)
  for (const row of visibleSelectableRows.value) {
    if (checked) next.add(row.key)
    else next.delete(row.key)
  }
  selectedKeys.value = next
}

function clearSelection() {
  selectedKeys.value = new Set()
}

function exitSelectionMode() {
  selectionMode.value = false
  clearSelection()
}

function toggleSelectionMode() {
  if (selectionMode.value) {
    exitSelectionMode()
    return
  }
  selectionMode.value = true
}

useDocumentEvent('keydown', (event) => {
  if (event.key !== 'Escape' || !selectionMode.value) return
  event.preventDefault()
  exitSelectionMode()
})

async function requestBulkDelete() {
  closeMenu()
  const keys = [...selectedKeys.value].filter(key => visibleSelectableKeySet.value.has(key))
  if (keys.length === 0) return
  const ok = await confirm({
    title: t('shared.sidebar.bulkDeleteTitle'),
    body: t('shared.sidebar.bulkDeleteBody', { count: keys.length }),
    primaryLabel: t('shared.sidebar.bulkDeleteConfirm'),
  })
  if (!ok) return
  clearSelection()
  selectionMode.value = false
  emit('bulk-delete', keys)
}

/* ── Per-row ⋯ menu + inline rename ────────────────────────────────── */

const openMenuKey = ref('')
// The ⋯ trigger that opened the active menu, captured so Escape can return
// focus to it. A function-ref on the single open .sidebar-row-menu scopes the
// roving-focus queries (only one menu renders at a time).
const menuTriggerEl = ref<HTMLElement | null>(null)
const openMenuEl = ref<HTMLElement | null>(null)
function setOpenMenu(el: Element | ComponentPublicInstance | null) {
  openMenuEl.value = el instanceof HTMLElement ? el : null
}
// Fixed-position style for the teleported menu, computed from the trigger rect
// on open so the menu escapes the Recents scroll-clip.
const menuStyle = ref<Record<string, string>>({})
const renamingKey = ref('')
const renameDraft = ref('')
// A function ref captures the single active rename input. A string ref inside
// the v-for would collect into an array even though only one input renders, so
// the explicit callback keeps a direct element handle for focus/select.
const renameInputEl = ref<HTMLInputElement | null>(null)
function setRenameInput(el: Element | ComponentPublicInstance | null) {
  renameInputEl.value = el instanceof HTMLInputElement ? el : null
}
// Guards the blur-saves behavior so an Enter/Esc keystroke does not also fire a
// duplicate save through the input's blur handler.
let renameCommitting = false

function toggleMenu(key: string, event?: Event) {
  if (openMenuKey.value === key) {
    closeMenu()
    return
  }
  openMenuKey.value = key
  const trigger = event?.currentTarget
  menuTriggerEl.value = trigger instanceof HTMLElement ? trigger : null
  // The menu is teleported to <body>; anchor it to the trigger, flipping upward
  // near the viewport bottom so the Delete item is never clipped off-screen.
  if (menuTriggerEl.value) {
    const r = menuTriggerEl.value.getBoundingClientRect()
    const openUp = r.bottom + 220 > window.innerHeight
    const isProjectMenu = Boolean(
      menuTriggerEl.value.closest('.sidebar-history-row--workspace'),
    )
    const openProjectMenuRight = isProjectMenu && r.right + 160 < window.innerWidth
    menuStyle.value = {
      position: 'fixed',
      left: `${openProjectMenuRight ? r.right + 6 : r.right}px`,
      top: `${openUp ? r.top : r.bottom + 4}px`,
      transform: openProjectMenuRight
        ? (openUp ? 'translateY(-100%)' : 'none')
        : (openUp ? 'translate(-100%, -100%)' : 'translateX(-100%)'),
    }
  }
  // Move focus into the menu so keyboard users land on an actionable item.
  nextTick(() => {
    const items = openMenuEl.value?.querySelectorAll<HTMLElement>('.sidebar-row-menu__item')
    items?.[0]?.focus()
  })
}

function closeMenu() {
  openMenuKey.value = ''
  openMenuEl.value = null
  menuTriggerEl.value = null
}

// Escape closes and returns focus to the row's ⋯ trigger; arrows rove between
// the menu items, wrapping at the ends.
function onMenuKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    const trigger = menuTriggerEl.value
    closeMenu()
    nextTick(() => trigger?.focus())
    return
  }
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
  const items = Array.from(
    openMenuEl.value?.querySelectorAll<HTMLElement>('.sidebar-row-menu__item') ?? [],
  )
  if (!items.length) return
  e.preventDefault()
  const current = items.indexOf(document.activeElement as HTMLElement)
  const delta = e.key === 'ArrowDown' ? 1 : -1
  const next = (current + delta + items.length) % items.length
  items[next]?.focus()
}

useDocumentEvent('click', (e) => {
  if (!openMenuKey.value) return
  if (e.target instanceof Node) {
    const host = (e.target as Element).closest?.('.sidebar-row-menu-wrap, .sidebar-row-menu')
    if (host) return
  }
  closeMenu()
})

function startRename(row: SidebarConversationItem) {
  closeMenu()
  renamingKey.value = row.key
  renameDraft.value = row.title
  renameCommitting = false
  nextTick(() => {
    renameInputEl.value?.focus()
    renameInputEl.value?.select()
  })
}

function commitRename() {
  if (renameCommitting) return
  const key = renamingKey.value
  if (!key) return
  renameCommitting = true
  const title = renameDraft.value.trim()
  const original = props.sections
    .flatMap(section => section.rows)
    .find(row => row.key === key)?.title || ''
  renamingKey.value = ''
  renameDraft.value = ''
  if (title && title !== original) emit('rename', { key, title })
}

function cancelRename() {
  renameCommitting = true
  renamingKey.value = ''
  renameDraft.value = ''
}

function onRenameBlur() {
  // Enter/Esc already settled this row; only a genuine focus-loss commits.
  if (renameCommitting) return
  commitRename()
}

async function requestDelete(row: SidebarConversationItem) {
  closeMenu()
  const ok = await confirm({
    title: t('shared.sidebar.deleteSessionTitle'),
    body: t('shared.sidebar.deleteSessionBody', { title: row.title }),
    primaryLabel: t('shared.sidebar.deleteSessionConfirm'),
  })
  if (!ok) return
  emit('delete', row.key)
}

function emitProjectPin(row: SidebarConversationItem) {
  closeMenu()
  if (!row.workspaceId) return
  emit('project-pin', {
    workspaceId: row.workspaceId,
    pinned: !row.workspacePinned,
  })
}

function emitProjectEdit(row: SidebarConversationItem) {
  closeMenu()
  if (row.workspaceId) emit('project-edit', row.workspaceId)
}

async function requestProjectHistoryDelete(row: SidebarConversationItem) {
  closeMenu()
  if (!row.workspaceId) return
  const ok = await confirm({
    title: t('workspaces.deleteHistoryTitle'),
    body: t('workspaces.deleteHistoryBody', {
      count: row.workspaceTaskCount ?? 0,
      name: row.title,
    }),
    primaryLabel: t('workspaces.deleteHistoryConfirm'),
    primaryClass: 'btn--danger',
  })
  if (ok) emit('project-delete-history', row.workspaceId)
}

function emitProjectRemove(row: SidebarConversationItem) {
  closeMenu()
  if (row.workspaceId) emit('project-remove', row.workspaceId)
}

function emitSessionPin(row: SidebarConversationItem) {
  closeMenu()
  if (row.rowKind === 'session') emit('session-pin', { key: row.key, pinned: !row.pinned })
}

function onSelectRow(row: SidebarConversationItem) {
  if (row.rowKind !== 'session') return
  if (suppressSelectKey.value === row.key) {
    suppressSelectKey.value = ''
    return
  }
  if (row.provisional) return
  if (renamingKey.value === row.key) return
  if (selectionMode.value) {
    setRowSelected(row.key, !isRowSelected(row.key))
    return
  }
  emit('select', row.key)
}
</script>

<template>
  <div
    v-if="error || totalRows > 0 || visibleProjectCount > 0 || props.canManageProjects"
    class="sidebar-section sidebar-history"
    :class="{
      'is-selecting': selectionMode,
      'has-projects': visibleProjectCount > 0,
    }"
    :aria-label="t('shared.sidebar.recentConversations')"
  >
    <div class="sidebar-recents-header">
      <span class="sidebar-recents-eyebrow">
        {{
          selectionMode
            ? selectedCount > 0
              ? t('shared.sidebar.selectedCountLabel', { count: selectedCount })
              : t('shared.sidebar.selectionModeLabel')
            : props.canManageProjects
              ? t('workspaces.projects')
              : t('shared.sidebar.recents')
        }}
      </span>
      <span
        v-if="!selectionMode && visibleProjectCount === 0 && totalRows > 0"
        class="sidebar-recents-count"
      >{{ totalRows }}</span>
      <button
        v-if="!selectionMode && props.canManageProjects && props.canCreateProjects"
        type="button"
        class="sidebar-project-create-btn"
        data-testid="sidebar-create-project"
        :aria-label="t('workspaces.createProject')"
        :title="t('workspaces.createProject')"
        @click="emit('new-project')"
      >
        <Icon name="plus" :size="13" />
      </button>
      <!-- Conversation search lives on the recents header, beside the selection
           and refresh controls, because the palette's hits are these rows.
           Hidden while selecting: that mode owns the header's spare width. -->
      <button
        v-if="!selectionMode"
        type="button"
        class="sidebar-cmd-btn"
        :aria-label="`${t('chrome.searchChats')} (${props.searchHint})`"
        :title="`${t('chrome.searchChats')} (${props.searchHint})`"
        aria-haspopup="dialog"
        @click="emit('search')"
      >
        <Icon name="search" :size="13" />
      </button>
      <button
        v-if="selectionMode"
        type="button"
        class="sidebar-select-all-btn"
        :disabled="visibleSelectableCount === 0"
        :aria-label="allVisibleSelected ? t('shared.sidebar.clearVisibleSelection') : t('shared.sidebar.selectVisible')"
        :title="allVisibleSelected ? t('shared.sidebar.clearVisibleSelection') : t('shared.sidebar.selectVisible')"
        @click="toggleVisibleSelection"
      >
        {{ allVisibleSelected ? t('shared.sidebar.clearAllShort') : t('shared.sidebar.selectAllShort') }}
      </button>
      <button
        v-if="selectionMode"
        type="button"
        class="sidebar-bulk-delete-btn"
        :disabled="selectedCount === 0"
        :aria-label="t('shared.sidebar.deleteSelectedAria', { count: selectedCount })"
        :title="t('shared.sidebar.deleteSelectedAria', { count: selectedCount })"
        @click="requestBulkDelete"
      >
        <Icon name="trash" :size="12" />
      </button>
      <button
        v-if="selectionMode"
        type="button"
        class="sidebar-selection-done-btn"
        :aria-label="t('shared.sidebar.exitSelectionMode')"
        :title="t('shared.sidebar.exitSelectionMode')"
        @click="exitSelectionMode"
      >
        {{ t('shared.sidebar.selectionDone') }}
      </button>
      <button
        v-if="totalRows > 0 && !selectionMode"
        type="button"
        class="sidebar-bulk-mode-btn"
        :aria-label="t('shared.sidebar.enterSelectionMode')"
        :title="t('shared.sidebar.enterSelectionMode')"
        @click="toggleSelectionMode"
      >
        <Icon name="listChecks" :size="13" />
      </button>
    </div>

    <div v-if="agentFilter" class="sidebar-filter-row">
      <button
        type="button"
        class="sidebar-agent-chip"
        :aria-label="t('shared.sidebar.clearAgentFilter', { name: agentFilterName })"
        @click="clearAgentFilter"
      >
        {{ agentFilterName }} <span aria-hidden="true">&times;</span>
      </button>
    </div>

    <!-- The header no longer carries a standing refresh control, so the retry
         lives here — the one moment it is actually needed. -->
    <div v-if="error" class="sidebar-history-empty">
      <p>{{ t('shared.sidebar.loadError') }}</p>
      <button
        type="button"
        class="sidebar-history-retry"
        :disabled="loading"
        @click="emit('refresh')"
      >
        {{ t('shared.sidebar.refresh') }}
      </button>
    </div>

    <!-- Filtered to nothing within the Chats agent filter -->
    <div v-else-if="agentFilter && !hasFilterMatches" class="sidebar-history-empty">
      {{ t('shared.sidebar.noMatches') }}
    </div>

    <div v-else class="sidebar-history-list">
      <div
        v-for="section in visibleSections"
        :key="section.family"
        class="sidebar-group"
        :data-family="section.family"
      >
        <!-- One vocabulary, one header: with a single family the panel's own
             "Chats" eyebrow already labels the list, so the per-family header
             renders only when there are actually multiple families to tell
             apart (chats vs cron vs channels). -->
        <button
          v-if="visibleSections.length > 1"
          type="button"
          class="sidebar-group__header"
          :aria-expanded="!isCollapsed(section.family)"
          :aria-controls="`sidebar-group-${section.family}`"
          @click="toggleSection(section.family)"
        >
          <Icon class="sidebar-group__chevron" name="chevronRight" :size="12" />
          <span class="sidebar-group__label">{{ section.label }}</span>
          <span class="sidebar-group__count">{{ section.rows.length }}</span>
        </button>

        <TransitionGroup
          v-show="visibleSections.length === 1 || !isCollapsed(section.family)"
          :id="`sidebar-group-${section.family}`"
          name="sidebar-row"
          tag="div"
          class="sidebar-group__body"
        >
          <div
            v-for="row in section.rows"
            :key="row.key"
            class="sidebar-history-row"
            :class="{
              'is-selected': row.rowKind === 'session' && isRowSelected(row.key),
              'sidebar-history-row--workspace': row.rowKind === 'workspace',
              'sidebar-history-row--workspace-empty': row.rowKind === 'workspace-empty',
              'sidebar-history-row--recent-start': row.key === firstRecentRowKey,
              'is-unavailable': row.rowKind === 'workspace' && row.workspaceAvailable === false,
              'is-reorderable': canDragRow(row),
              'is-dragging': draggedRowKey === row.key,
              'is-drop-before': dropTargetKey === row.key && dropPosition === 'before',
              'is-drop-after': dropTargetKey === row.key && dropPosition === 'after',
            }"
            :data-family="section.family"
            :data-sidebar-zone="projectRowKeys.has(row.key) ? 'projects' : 'recents'"
            :data-zone-label="row.key === firstRecentRowKey ? t('shared.sidebar.recents') : undefined"
            :data-depth="row.depth"
            :data-session-key="row.rowKind === 'session' ? row.key : undefined"
            :style="{ '--row-depth': row.depth }"
            @pointerdown="onRowPointerDown(row, $event)"
          >
            <div
              v-if="row.rowKind === 'workspace'"
              class="sidebar-workspace-header"
            >
              <div class="sidebar-project-info-wrap">
                <button
                  type="button"
                  class="sidebar-project-info"
                  data-testid="project-workspace-info"
                  :aria-label="t('workspaces.projectInfo', {
                    path: row.workspaceDisplayPath || row.workspace || row.title,
                    count: row.workspaceTaskCount ?? 0,
                  })"
                >
                  <Icon name="folder" :size="15" />
                </button>
                <div class="sidebar-project-info-popover" role="tooltip">
                  <span class="sidebar-project-info-path">
                    {{ row.workspaceDisplayPath || row.workspace || row.title }}
                  </span>
                  <span>{{ t('workspaces.taskCount', { count: row.workspaceTaskCount ?? 0 }) }}</span>
                  <span v-if="row.workspaceAvailable === false" class="sidebar-project-unavailable">
                    {{ t('workspaces.unavailable') }}
                  </span>
                </div>
              </div>
              <button
                type="button"
                class="sidebar-project-disclosure"
                data-testid="project-workspace-disclosure"
                :aria-expanded="!isProjectCollapsed(row)"
                :aria-label="row.title"
                @click="toggleProject(row)"
              >
                <Icon class="sidebar-project-chevron" name="chevronRight" :size="12" />
                <span class="sidebar-workspace-label">{{ row.title }}</span>
              </button>
              <div
                v-if="!selectionMode && props.canManageProjects && row.workspaceId"
                class="sidebar-project-actions"
              >
                <button
                  type="button"
                  class="sidebar-project-action sidebar-project-action--new-task"
                  data-testid="project-workspace-new-task"
                  :aria-label="row.workspaceAvailable === false
                    ? t('workspaces.unavailableProjectCannotStartTask')
                    : t('workspaces.newTask')"
                  :title="row.workspaceAvailable === false
                    ? t('workspaces.unavailableProjectCannotStartTask')
                    : t('workspaces.newTask')"
                  :disabled="row.workspaceAvailable === false"
                  @click.stop="startProjectTask(row)"
                >
                  <Icon name="plus" :size="13" />
                </button>
                <button
                  type="button"
                  class="sidebar-project-action sidebar-row-menu-btn"
                  data-testid="project-workspace-more"
                  aria-haspopup="menu"
                  :aria-expanded="openMenuKey === row.key"
                  :aria-label="t('workspaces.moreActions')"
                  :title="t('workspaces.moreActions')"
                  @click.stop="toggleMenu(row.key, $event)"
                >
                  <Icon name="moreHorizontal" :size="14" />
                </button>
              </div>
            </div>

            <span
              v-if="row.depth > 0 && row.rowKind !== 'workspace'"
              class="sidebar-history-rail"
              aria-hidden="true"
            />

            <div
              v-if="row.rowKind === 'workspace-empty'"
              class="sidebar-workspace-empty"
            >
              {{ row.title }}
            </div>

            <!-- Inline rename input replaces the row button while editing -->
            <input
              v-if="row.rowKind === 'session' && renamingKey === row.key"
              :ref="setRenameInput"
              v-model="renameDraft"
              class="sidebar-history-rename"
              type="text"
              :aria-label="t('shared.sidebar.renameLabel', { title: row.title })"
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="cancelRename"
              @blur="onRenameBlur"
            />

            <button
              v-else-if="row.rowKind === 'session'"
              class="sidebar-history-item"
              :class="{ 'is-current': row.key === currentKey }"
              :title="row.title"
              :aria-pressed="selectionMode && !row.provisional ? isRowSelected(row.key) : undefined"
              @click="onSelectRow(row)"
            >
              <span
                v-if="selectionMode && !row.provisional"
                class="sidebar-selection-box"
                :class="{ 'is-checked': isRowSelected(row.key) }"
                aria-hidden="true"
              >
                <Icon v-if="isRowSelected(row.key)" name="check" :size="11" />
              </span>
              <span class="sidebar-history-title">{{ row.title }}</span>
              <Icon
                v-if="row.pinned"
                class="sidebar-history-pin"
                name="arrowUp"
                :size="11"
                aria-hidden="true"
              />
              <span
                v-if="contractDebugEnabled && row.hasContractGaps"
                class="sidebar-history-gap"
                :aria-label="t('shared.sidebar.contractGap')"
                :title="t('shared.sidebar.contractGap')"
              >{{ t('shared.sidebar.contractGapBadge') }}</span>
              <span
                v-if="!selectionMode"
                class="sidebar-task-attention"
                :class="`sidebar-task-attention--${row.taskAttention}`"
                :role="row.taskAttention === 'none' ? undefined : 'img'"
                :aria-hidden="row.taskAttention === 'none' ? 'true' : undefined"
                :aria-label="taskAttentionLabel(row.taskAttention) || undefined"
                :title="taskAttentionLabel(row.taskAttention) || undefined"
                data-testid="sidebar-task-attention"
              />
            </button>

            <!-- Chat-only ⋯ menu: rename + delete -->
            <Teleport to="body">
              <div
                v-if="
                  props.canManageProjects
                  && row.rowKind === 'workspace'
                  && openMenuKey === row.key
                "
                :ref="setOpenMenu"
                class="sidebar-row-menu sidebar-project-menu"
                :style="menuStyle"
                role="menu"
                :aria-label="t('workspaces.moreActions')"
                @keydown="onMenuKeydown"
              >
                <button
                  type="button"
                  class="sidebar-row-menu__item"
                  data-project-action="pin"
                  role="menuitem"
                  @click.stop="emitProjectPin(row)"
                >
                  <Icon name="arrowUp" :size="13" />
                  <span>{{ row.workspacePinned ? t('workspaces.unpin') : t('workspaces.pin') }}</span>
                </button>
                <button
                  type="button"
                  class="sidebar-row-menu__item"
                  data-project-action="edit"
                  role="menuitem"
                  @click.stop="emitProjectEdit(row)"
                >
                  <Icon name="pencil" :size="13" />
                  <span>{{ t('workspaces.editProject') }}</span>
                </button>
                <button
                  type="button"
                  class="sidebar-row-menu__item"
                  data-project-action="delete-history"
                  role="menuitem"
                  @click.stop="requestProjectHistoryDelete(row)"
                >
                  <Icon name="trash" :size="13" />
                  <span>{{ t('workspaces.menuDeleteHistory') }}</span>
                </button>
                <button
                  type="button"
                  class="sidebar-row-menu__item"
                  data-project-action="remove"
                  role="menuitem"
                  @click.stop="emitProjectRemove(row)"
                >
                  <Icon name="x" :size="13" />
                  <span>{{ t('workspaces.menuRemove') }}</span>
                </button>
              </div>
            </Teleport>

            <div
              v-if="
                row.rowKind === 'session'
                && (
                  row.sessionKind === 'chat'
                  || row.sessionKind === 'cron'
                  || row.sessionKind === 'channel'
                )
                && !row.provisional
                && renamingKey !== row.key
                && !selectionMode
              "
              class="sidebar-row-menu-wrap"
            >
              <button
                type="button"
                class="sidebar-row-menu-btn"
                aria-haspopup="menu"
                :aria-expanded="openMenuKey === row.key"
                :aria-label="t('shared.sidebar.rowActions', { title: row.title })"
                :title="t('shared.sidebar.rowActions', { title: row.title })"
                @click.stop="toggleMenu(row.key, $event)"
              >
                <span aria-hidden="true">&#8943;</span>
              </button>
              <Teleport to="body">
              <div
                v-if="openMenuKey === row.key"
                :ref="setOpenMenu"
                class="sidebar-row-menu"
                :style="menuStyle"
                role="menu"
                :aria-label="t('shared.sidebar.rowActions', { title: row.title })"
                @keydown="onMenuKeydown"
              >
                <button
                  type="button"
                  class="sidebar-row-menu__item"
                  role="menuitem"
                  @click.stop="emitSessionPin(row)"
                >
                  <Icon name="arrowUp" :size="14" />
                  <span>{{ row.pinned ? t('shared.sidebar.unpinTask') : t('shared.sidebar.pinTask') }}</span>
                </button>
                <button
                  type="button"
                  class="sidebar-row-menu__item"
                  role="menuitem"
                  @click.stop="startRename(row)"
                >
                  <Icon name="pencil" :size="14" />
                  <span>{{ t('shared.sidebar.rename') }}</span>
                </button>
                <button
                  type="button"
                  class="sidebar-row-menu__item sidebar-row-menu__item--danger"
                  role="menuitem"
                  @click.stop="requestDelete(row)"
                >
                  <Icon name="trash" :size="14" />
                  <span>{{ t('shared.sidebar.delete') }}</span>
                </button>
              </div>
              </Teleport>
            </div>

            <!-- Agent-initial badge: indicator + click-to-filter (Chats only) -->
            <button
              v-else-if="
                row.rowKind === 'session'
                && !row.provisional
                && shouldShowAgentFilterBadge(section.family, row)
                && renamingKey !== row.key
                && !selectionMode
              "
              type="button"
              class="sidebar-agent-badge"
              :class="{ 'is-active': agentFilter === row.effectiveAgentId }"
              :aria-pressed="agentFilter === row.effectiveAgentId"
              :aria-label="t('shared.sidebar.filterByAgent', { name: row.agentName })"
              :title="t('shared.sidebar.filterByAgent', { name: row.agentName })"
              @click.stop="toggleAgentFilter(row.effectiveAgentId)"
            >
              {{ agentInitial(row.agentName) }}
            </button>
          </div>
        </TransitionGroup>
      </div>
      <div
        v-if="visibleProjectCount > 0 && !hasVisibleRecentRows"
        class="sidebar-zone-empty"
      >
        <div class="sidebar-zone-empty__label">{{ t('shared.sidebar.recents') }}</div>
        <div class="sidebar-zone-empty__body">{{ t('shared.sidebar.noConversations') }}</div>
      </div>
    </div>
  </div>
</template>
