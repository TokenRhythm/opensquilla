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
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  ref,
  watch,
  type ComponentPublicInstance,
} from 'vue'
import { useI18n } from 'vue-i18n'
import type { SessionTaskAttention } from '@/composables/useSessionTaskAttention'
import Icon from './Icon.vue'
import SidebarSessionDragPreview from './SidebarSessionDragPreview.vue'
import SidebarSessionHoverCard, {
  canShowSessionPreview,
  sessionPreviewPosition,
} from './SidebarSessionHoverCard.vue'
import { useConfirm } from '@/composables/useConfirm'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { usePlatform } from '@/platform'
import { useSidebarVirtualizer } from '@/composables/useSidebarVirtualizer'
import { shouldShowAgentFilterBadge } from '@/utils/sidebarConversations'
import { buildSidebarTaskHierarchy } from '@/utils/sidebarTaskHierarchy'
import {
  buildSidebarDisplayProjection,
  isSidebarSessionOrderable,
  type SidebarDisplayRow,
  type SidebarDisplayZone,
} from '@/utils/sidebarDisplayProjection'

const props = withDefaults(defineProps<{
  sections: SidebarSection[]
  error: boolean
  loading: boolean
  loadingMore?: boolean
  loadMoreError?: boolean
  hasMore?: boolean
  currentKey: string
  contractDebugEnabled: boolean
  /** Command-palette chord, shown in the search button's tooltip. */
  searchHint: string
  sessionOrder?: string[]
  canManageProjects?: boolean
  canCreateProjects?: boolean
}>(), {
  sessionOrder: () => [],
  loadingMore: false,
  loadMoreError: false,
  hasMore: false,
  canManageProjects: false,
  canCreateProjects: false,
})

const isDesktop = usePlatform().capabilities.isDesktop

const emit = defineEmits<{
  (e: 'select', key: string): void
  (e: 'refresh'): void
  (e: 'load-more'): void
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
const historyList = ref<HTMLElement | null>(null)

function maybeLoadMore() {
  const element = historyList.value
  if (
    !element
    || !props.hasMore
    || props.loading
    || props.loadingMore
    || props.loadMoreError
  ) return
  const remaining = sidebarVirtualizer.distanceFromEnd()
  if (remaining <= 160) emit('load-more')
}

function onHistoryScroll() {
  updateRowDropTarget()
  maybeLoadMore()
}

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

function filterCollapsedProjectRows<T extends SidebarConversationItem>(rows: T[]): T[] {
  const hiddenProjects = new Set<string>()
  const result: T[] = []
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
const filteredSections = computed(() => {
  return props.sections
    .map(section => {
      const filteredRows = section.family === 'chats' && agentFilter.value
        ? filterChatRowsByAgent(section.rows, agentFilter.value)
        : section.rows
      return {
        ...section,
        rows: filteredRows.filter(row => row.rowKind !== 'workspace-empty'),
      }
    })
    .filter(section => section.rows.length > 0)
})

const displayProjection = computed(() =>
  buildSidebarDisplayProjection(filteredSections.value, props.sessionOrder),
)

const taskHierarchy = computed(() => buildSidebarTaskHierarchy(displayProjection.value.allRows))

function taskCollapseKey(key: string): string {
  return `task:${key}`
}

function isTaskCollapsed(key: string): boolean {
  const saved = collapsed.value[taskCollapseKey(key)]
  return typeof saved === 'boolean'
    ? saved
    : taskHierarchy.value.summaries.get(key)?.allFinished === true
}

function toggleTask(row: SidebarDisplayRow) {
  closeSessionPreview()
  const next = { ...collapsed.value, [taskCollapseKey(row.key)]: !isTaskCollapsed(row.key) }
  collapsed.value = next
  writeSidebarCollapsedState(next)
}

function filterCollapsedTaskRows<T extends SidebarDisplayRow>(rows: T[]): T[] {
  return rows.filter(row => row.pinned || !(taskHierarchy.value.ancestors.get(row.key) ?? [])
    .some(isTaskCollapsed))
}

function subtaskSummaryLabel(key: string): string {
  const summary = taskHierarchy.value.summaries.get(key)
  if (!summary) return ''
  const labels = [t('shared.sidebar.subtaskCount', { count: summary.count })]
  if (summary.running) labels.push(t('shared.sidebar.subtasksRunning', { count: summary.running }))
  if (summary.attention) labels.push(t('shared.sidebar.subtasksAttention', { count: summary.attention }))
  return labels.join(' · ')
}

// Route navigation and newly loaded lineage reveal the selected task. Explicit
// manual collapse remains possible until the user navigates to another task.
watch(
  () => {
    const row = findSessionRow(props.currentKey)
    return [props.currentKey, row?.workspaceId, row?.displayFamily,
      ...(taskHierarchy.value.ancestors.get(props.currentKey) ?? [])].join('\u0000')
  },
  () => {
    const row = findSessionRow(props.currentKey)
    if (!row) return
    const next = { ...collapsed.value, [row.displayFamily]: false }
    for (const ancestor of taskHierarchy.value.ancestors.get(row.key) ?? []) {
      next[taskCollapseKey(ancestor)] = false
    }
    if (row.workspaceId) next[projectCollapseKey(row.workspaceId)] = false
    collapsed.value = next
    writeSidebarCollapsedState(next)
  },
  { immediate: true },
)

interface SidebarDisplayBlock {
  key: string
  zone: SidebarDisplayZone
  label: string
  count: number
  rows: SidebarDisplayRow[]
  showHeading: boolean
  family?: SidebarFamilyId
  familyLabel?: string
  showFamilyHeader?: boolean
}

const displayBlocks = computed<SidebarDisplayBlock[]>(() => {
  const projection = displayProjection.value
  const blocks: SidebarDisplayBlock[] = []
  if (projection.pinned.length > 0) {
    blocks.push({
      key: 'pinned',
      zone: 'pinned',
      label: t('shared.sidebar.pinned'),
      count: projection.pinned.length,
      rows: projection.pinned,
      showHeading: true,
    })
  }
  if (props.canManageProjects || projection.projectCount > 0) {
    blocks.push({
      key: 'projects',
      zone: 'projects',
      label: t('workspaces.projects'),
      count: projection.projectCount,
      rows: filterCollapsedTaskRows(filterCollapsedProjectRows(projection.projects)),
      showHeading: true,
    })
  }
  if (projection.recents.length === 0) {
    blocks.push({
      key: 'recents',
      zone: 'recents',
      label: t('shared.sidebar.recents'),
      count: 0,
      rows: [],
      showHeading: true,
    })
  } else {
    projection.recents.forEach((section, index) => {
      blocks.push({
        key: `recents:${section.family}`,
        zone: 'recents',
        label: t('shared.sidebar.recents'),
        count: projection.recentCount,
        rows: filterCollapsedTaskRows(section.rows),
        showHeading: index === 0,
        family: section.family,
        familyLabel: section.label,
        showFamilyHeader: projection.recents.length > 1,
      })
    })
  }
  return blocks
})

const controlsZone = computed<SidebarDisplayZone>(() =>
  props.canManageProjects || displayProjection.value.projectCount > 0
    ? 'projects'
    : 'recents',
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
  filteredSections.value.some(section => section.rows.some(row => !isWorkspaceRow(row))),
)

/* ── Session drag ordering ────────────────────────────────────────── */

const draggedRowKey = ref('')
const dropTargetKey = ref('')
const dropPosition = ref<'before' | 'after'>('before')
const pointerDrag = ref<{
  key: string
  title: string
  scope: string
  pointerId: number
  source: HTMLElement
  startX: number
  startY: number
  clientX: number
  clientY: number
  width: number
  height: number
  active: boolean
} | null>(null)
const suppressSelectKey = ref('')
const settlingRowKey = ref('')
let dragScrollFrame = 0
let dragScrollTime = 0
let settlingRowTimer: ReturnType<typeof setTimeout> | undefined

function reorderScope(row: SidebarDisplayRow): string {
  if (row.pinned) return 'pinned'
  if (row.displayZone === 'recents') return 'recents'
  return `project:${row.workspaceId || row.workspace || ''}`
}

function canDragRow(row: SidebarDisplayRow): boolean {
  return isSidebarSessionOrderable(row)
    && !selectionMode.value
    && !agentFilter.value
    && renamingKey.value !== row.key
}

function keyboardReorderTarget(row: SidebarDisplayRow, direction: 'up' | 'down'): SidebarDisplayRow | undefined {
  if (!canDragRow(row)) return undefined
  const siblings = displayBlocks.value.flatMap(block => block.rows)
    .filter(item => canDragRow(item) && reorderScope(item) === reorderScope(row))
  const index = siblings.findIndex(item => item.key === row.key)
  return index < 0 ? undefined : siblings[index + (direction === 'up' ? -1 : 1)]
}

function reorderFromMenu(row: SidebarDisplayRow, direction: 'up' | 'down') {
  const target = keyboardReorderTarget(row, direction)
  if (!target) return
  const trigger = menuTriggerEl.value
  focusedItemKey.value = `row:${row.key}`
  closeMenu()
  settleRow(row.key)
  emit('reorder', { draggedKey: row.key, targetKey: target.key, position: direction === 'up' ? 'before' : 'after' })
  nextTick(() => trigger?.focus())
}

function clearRowDrag() {
  const drag = pointerDrag.value
  if (drag?.active) suppressSelectKey.value = drag.key
  if (dragScrollFrame) cancelAnimationFrame(dragScrollFrame)
  dragScrollFrame = 0
  dragScrollTime = 0
  draggedRowKey.value = ''
  dropTargetKey.value = ''
  pointerDrag.value = null
  if (drag?.source.hasPointerCapture?.(drag.pointerId)) {
    drag.source.releasePointerCapture(drag.pointerId)
  }
}

function settleRow(key: string) {
  settlingRowKey.value = key
  if (settlingRowTimer) clearTimeout(settlingRowTimer)
  settlingRowTimer = setTimeout(() => {
    if (settlingRowKey.value === key) settlingRowKey.value = ''
    settlingRowTimer = undefined
  }, 360)
}

function findSessionRow(key: string): SidebarDisplayRow | undefined {
  return displayProjection.value.allRows.find(row => row.key === key)
}

function onRowPointerDown(row: SidebarDisplayRow, event: PointerEvent) {
  if (pointerDrag.value) return
  // A new gesture (including a touch tap) cannot be the click left by a drag.
  suppressSelectKey.value = ''
  // Touch belongs to the scroll container; a swipe must never reorder a task.
  if (event.button !== 0 || event.pointerType === 'touch' || !canDragRow(row)) return
  const target = event.target
  if (target instanceof Element && target.closest('.sidebar-row-menu-wrap, input, .sidebar-agent-badge, .sidebar-task-disclosure')) return
  const source = event.currentTarget
  if (!(source instanceof HTMLElement)) return
  const rect = source.getBoundingClientRect()
  pointerDrag.value = {
    key: row.key,
    title: row.title,
    scope: reorderScope(row),
    pointerId: event.pointerId,
    source,
    startX: event.clientX,
    startY: event.clientY,
    clientX: event.clientX,
    clientY: event.clientY,
    width: rect.width,
    height: rect.height,
    active: false,
  }
}

function updateRowDropTarget() {
  const drag = pointerDrag.value
  if (!drag?.active) return
  const sourceRow = findSessionRow(drag.key)
  const target = document.elementFromPoint(drag.clientX, drag.clientY)
    ?.closest<HTMLElement>('.sidebar-history-row[data-session-key]')
  const targetKey = target?.dataset.sessionKey || ''
  const row = findSessionRow(targetKey)
  if (
    !sourceRow || !canDragRow(sourceRow) || reorderScope(sourceRow) !== drag.scope
    || !historyList.value?.contains(drag.source)
    || !target || !historyList.value?.contains(target) || !row
    || row.key === drag.key || !canDragRow(row) || reorderScope(row) !== drag.scope
  ) {
    dropTargetKey.value = ''
    return
  }
  const rect = target.getBoundingClientRect()
  dropTargetKey.value = row.key
  dropPosition.value = drag.clientY < rect.top + rect.height / 2 ? 'before' : 'after'
}

function scrollDuringRowDrag(time: number) {
  const drag = pointerDrag.value
  const list = historyList.value
  if (!drag?.active || !list) return
  const elapsed = dragScrollTime ? Math.min(time - dragScrollTime, 32) : 16
  dragScrollTime = time
  const rect = list.getBoundingClientRect()
  const edge = Math.min(48, rect.height / 4)
  if (edge > 0 && drag.clientX >= rect.left && drag.clientX <= rect.right) {
    const up = Math.max(0, Math.min(1, (rect.top + edge - drag.clientY) / edge))
    const down = Math.max(0, Math.min(1, (drag.clientY - rect.bottom + edge) / edge))
    const before = list.scrollTop
    list.scrollTop += (down - up) * elapsed * 0.5
    if (list.scrollTop !== before) updateRowDropTarget()
  }
  dragScrollFrame = requestAnimationFrame(scrollDuringRowDrag)
}

useDocumentEvent('pointermove', (event) => {
  const drag = pointerDrag.value
  if (!drag || event.pointerId !== drag.pointerId) return
  if (!historyList.value?.contains(drag.source)) {
    clearRowDrag()
    return
  }
  drag.clientX = event.clientX
  drag.clientY = event.clientY
  if (!drag.active) {
    if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 6) return
    drag.active = true
    draggedRowKey.value = drag.key
    closeSessionPreview()
    closeMenu()
    // Capturing keeps release/cancellation reliable outside the sidebar.
    if (Number.isFinite(drag.pointerId)) drag.source.setPointerCapture?.(drag.pointerId)
    dragScrollFrame = requestAnimationFrame(scrollDuringRowDrag)
  }
  event.preventDefault()
  updateRowDropTarget()
}, { passive: false })

useDocumentEvent('pointerup', (event) => {
  const drag = pointerDrag.value
  if (!drag || event.pointerId !== drag.pointerId) return
  if (drag.active) {
    drag.clientX = event.clientX
    drag.clientY = event.clientY
    updateRowDropTarget()
    if (dropTargetKey.value) {
      settleRow(drag.key)
      emit('reorder', {
        draggedKey: drag.key,
        targetKey: dropTargetKey.value,
        position: dropPosition.value,
      })
    }
  }
  clearRowDrag()
})

useDocumentEvent('pointercancel', (event) => {
  if (event.pointerId === pointerDrag.value?.pointerId) clearRowDrag()
})
useDocumentEvent('lostpointercapture', (event) => {
  if (event.pointerId === pointerDrag.value?.pointerId) clearRowDrag()
})
useDocumentEvent('keydown', (event) => {
  if (event.key !== 'Escape' || !pointerDrag.value) return
  event.preventDefault()
  clearRowDrag()
})
onMounted(() => window.addEventListener('blur', clearRowDrag))
onUnmounted(() => {
  window.removeEventListener('blur', clearRowDrag)
  if (settlingRowTimer) clearTimeout(settlingRowTimer)
  clearRowDrag()
})

/* ── Bulk selection ───────────────────────────────────────────────── */

const selectedKeys = ref<Set<string>>(new Set())
const selectionMode = ref(false)

const visibleSelectableRows = computed(() =>
  displayBlocks.value.flatMap(block =>
    block.showFamilyHeader && block.family && isCollapsed(block.family)
      ? []
      : block.rows.filter(row => row.rowKind === 'session' && !row.provisional),
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

function canOpenSessionMenu(row: SidebarDisplayRow): boolean {
  return row.rowKind === 'session'
    && (
      row.sessionKind === 'chat'
      || row.sessionKind === 'cron'
      || row.sessionKind === 'channel'
      || row.sessionKind === 'task'
    )
    && !row.provisional
    && renamingKey.value !== row.key
    && !selectionMode.value
}

function focusOpenMenu() {
  nextTick(() => {
    const items = openMenuEl.value?.querySelectorAll<HTMLElement>('.sidebar-row-menu__item')
    items?.[0]?.focus()
  })
}

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
  focusOpenMenu()
}

function openSessionContextMenu(row: SidebarDisplayRow, event: MouseEvent) {
  // Keep the browser's native context menu on the web build. The desktop shell
  // owns right-click behavior and reuses the same actions as the row's ⋯ menu.
  if (!isDesktop || !canOpenSessionMenu(row)) return
  event.preventDefault()
  event.stopPropagation()
  closeSessionPreview()
  openMenuKey.value = row.key
  const trigger = event.currentTarget
  menuTriggerEl.value = trigger instanceof HTMLElement ? trigger : null
  const openLeft = event.clientX + 160 > window.innerWidth
  const openUp = event.clientY + 220 > window.innerHeight
  menuStyle.value = {
    position: 'fixed',
    left: `${event.clientX}px`,
    top: `${event.clientY}px`,
    transform: openLeft
      ? (openUp ? 'translate(-100%, -100%)' : 'translateX(-100%)')
      : (openUp ? 'translateY(-100%)' : 'none'),
  }
  focusOpenMenu()
}

function closeMenu() {
  openMenuKey.value = ''
  openMenuEl.value = null
  menuTriggerEl.value = null
}

const sessionPreview = ref<{
  row: SidebarDisplayRow
  position: { left: string; top: string }
} | null>(null)

function openSessionPreview(row: SidebarDisplayRow, event: Event) {
  if (
    row.rowKind !== 'session'
    || selectionMode.value
    || draggedRowKey.value
    || openMenuKey.value
    || renamingKey.value === row.key
  ) return
  // On narrow/mobile layouts the sidebar fills the viewport. A fixed preview
  // would cover neighboring rows and make the list hard to operate.
  if (!canShowSessionPreview(window.innerWidth)) {
    closeSessionPreview()
    return
  }
  const anchor = event.currentTarget
  if (!(anchor instanceof HTMLElement)) return
  sessionPreview.value = {
    row,
    position: sessionPreviewPosition(
      anchor.getBoundingClientRect(),
      { width: window.innerWidth, height: window.innerHeight },
    ),
  }
}

function closeSessionPreview() {
  sessionPreview.value = null
}

function onSessionFocusOut(event: FocusEvent) {
  const row = event.currentTarget
  const next = event.relatedTarget
  if (row instanceof HTMLElement && next instanceof Node && row.contains(next)) return
  closeSessionPreview()
}

watch([selectionMode, openMenuKey], closeSessionPreview)
useDocumentEvent('scroll', closeSessionPreview, true)
onMounted(() => {
  window.addEventListener('resize', closeSessionPreview)
  void nextTick(maybeLoadMore)
})
onUnmounted(() => window.removeEventListener('resize', closeSessionPreview))
watch(
  [displayBlocks, () => props.hasMore, () => props.loadingMore],
  () => nextTick(maybeLoadMore),
  { flush: 'post' },
)

// Escape closes and returns focus to the row's ⋯ trigger; arrows rove between
// the menu items, wrapping at the ends.
function onMenuKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    const trigger = menuTriggerEl.value
    focusedItemKey.value = `row:${openMenuKey.value}`
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

function restoreRowFocus(key: string) {
  focusedItemKey.value = `row:${key}`
  void nextTick(() => {
    const row = [...(historyList.value?.querySelectorAll<HTMLElement>('[data-sidebar-item-key]') ?? [])]
      .find(element => element.dataset.sidebarItemKey === `row:${key}`)
    row?.querySelector<HTMLButtonElement>('.sidebar-history-item')?.focus({ preventScroll: true })
  })
}

function commitRename(restoreFocus = false) {
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
  if (restoreFocus) restoreRowFocus(key)
}

function cancelRename(restoreFocus = true) {
  const key = renamingKey.value
  renameCommitting = true
  renamingKey.value = ''
  renameDraft.value = ''
  if (restoreFocus && key) restoreRowFocus(key)
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

function onSelectRow(row: SidebarConversationItem, event: MouseEvent) {
  if (row.rowKind !== 'session') return
  if (suppressSelectKey.value === row.key) {
    suppressSelectKey.value = ''
    // Keyboard and assistive activation have no pointer click count. Pointer
    // capture can send the preceding drag click to the row wrapper instead.
    if (event.detail !== 0) return
  }
  if (row.provisional) return
  if (renamingKey.value === row.key) return
  if (selectionMode.value) {
    setRowSelected(row.key, !isRowSelected(row.key))
    return
  }
  emit('select', row.key)
}

const focusedItemKey = ref('')
const sidebarVirtualizer = useSidebarVirtualizer(
  historyList,
  computed(() => displayBlocks.value.map(block => ({
    ...block,
    collapsed: Boolean(block.showFamilyHeader && block.family && isCollapsed(block.family)),
  }))),
  computed(() => [renamingKey.value, openMenuKey.value, pointerDrag.value?.key || '', settlingRowKey.value]),
  focusedItemKey,
)
const { renderedBlocks, virtualized } = sidebarVirtualizer
function measureSidebarItem(value: Element | ComponentPublicInstance | null) {
  sidebarVirtualizer.measureElement(value instanceof HTMLElement ? value : null)
}
function rememberSidebarFocus(event: FocusEvent) {
  const target = event.target
  focusedItemKey.value = target instanceof Element
    ? target.closest<HTMLElement>('[data-sidebar-item-key]')?.dataset.sidebarItemKey || '' : ''
}
function releaseSidebarFocus(event: FocusEvent) {
  // activeElement can temporarily be body between native focusout/focusin.
  // Keep the destination leased through that handoff, before Vue can unmount it.
  if (event.relatedTarget instanceof Node && historyList.value?.contains(event.relatedTarget)) return
  void nextTick(() => {
    if (!historyList.value?.contains(document.activeElement)) focusedItemKey.value = ''
  })
}
// Reveal a route selected by search / navigation, but not every reorder or
// metadata refresh. Revisit an initial provisional row when the first page
// arrives and windowing starts. A manually collapsed row remains collapsed.
watch([
  () => props.currentKey,
  () => sidebarVirtualizer.hasRow(props.currentKey),
  virtualized,
  () => Boolean(findSessionRow(props.currentKey)?.provisional),
  historyList,
],
  ([key, present]) => { if (present) void sidebarVirtualizer.revealRow(key) },
  { immediate: true, flush: 'post' },
)
watch(() => sidebarVirtualizer.hasRow(renamingKey.value), present => {
  if (!present && renamingKey.value) cancelRename(false)
}, { flush: 'pre' })
watch(() => sidebarVirtualizer.hasRow(openMenuKey.value), present => {
  if (!present && openMenuKey.value) closeMenu()
}, { flush: 'pre' })
watch(() => sidebarVirtualizer.hasRow(pointerDrag.value?.key || ''), present => {
  if (!present && pointerDrag.value) clearRowDrag()
}, { flush: 'pre' })
</script>

<template>
  <div
    v-if="
      error
      || hasMore
      || loadingMore
      || loadMoreError
      || totalRows > 0
      || displayProjection.projectCount > 0
      || props.canManageProjects
    "
    class="sidebar-section sidebar-history"
    :class="{
      'is-selecting': selectionMode,
      'is-reordering': Boolean(draggedRowKey),
      'has-projects': displayProjection.projectCount > 0,
    }"
    :aria-label="t('shared.sidebar.recentConversations')"
  >
    <div v-if="selectionMode" class="sidebar-recents-header">
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
        v-if="!selectionMode && displayProjection.projectCount === 0 && totalRows > 0"
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
    <div
      v-if="agentFilter && !hasFilterMatches && !hasMore && !error"
      class="sidebar-history-empty"
    >
      {{ t('shared.sidebar.noMatches') }}
    </div>

    <div
      v-else-if="!error || totalRows > 0 || displayProjection.projectCount > 0"
      ref="historyList"
      class="sidebar-history-list"
      :data-sidebar-virtualized="virtualized"
      :data-sidebar-loaded-count="totalRows"
      @scroll.passive="onHistoryScroll"
      @focusin="rememberSidebarFocus"
      @focusout="releaseSidebarFocus"
    >
      <div
        v-for="{ block, headingIndex, rows, gapAfter } in renderedBlocks"
        :key="block.key"
        class="sidebar-group sidebar-zone"
        :data-family="block.family || block.key"
        :data-sidebar-zone-group="block.zone"
      >
        <div
          :ref="measureSidebarItem"
          :data-index="headingIndex"
          :data-sidebar-item-key="`heading:${block.key}`"
          class="sidebar-virtual-heading"
        >
        <div
          v-if="block.showHeading"
          class="sidebar-zone-heading"
          :data-sidebar-zone-heading="block.zone"
        >
          <span class="sidebar-zone-heading__label">{{ block.label }}</span>
          <span class="sidebar-zone-heading__count">{{ block.count }}</span>
          <button
            v-if="
              block.zone === 'projects'
              && props.canManageProjects
              && props.canCreateProjects
              && !selectionMode
            "
            type="button"
            class="sidebar-project-create-btn"
            data-testid="sidebar-create-project"
            :aria-label="t('workspaces.createProject')"
            :title="t('workspaces.createProject')"
            @click="emit('new-project')"
          >
            <Icon name="plus" :size="13" />
          </button>
          <button
            v-if="block.zone === controlsZone && !selectionMode"
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
            v-if="block.zone === controlsZone && totalRows > 0 && !selectionMode"
            type="button"
            class="sidebar-bulk-mode-btn"
            :aria-label="t('shared.sidebar.enterSelectionMode')"
            :title="t('shared.sidebar.enterSelectionMode')"
            @click="toggleSelectionMode"
          >
            <Icon name="listChecks" :size="13" />
          </button>
        </div>

        <button
          v-if="block.showFamilyHeader && block.family"
          type="button"
          class="sidebar-group__header"
          :aria-expanded="!isCollapsed(block.family)"
          :aria-controls="`sidebar-group-${block.key}`"
          @click="toggleSection(block.family)"
        >
          <Icon class="sidebar-group__chevron" name="chevronRight" :size="12" />
          <span class="sidebar-group__label">{{ block.familyLabel }}</span>
          <span class="sidebar-group__count">{{ block.rows.length }}</span>
        </button>
        <div v-if="block.zone === 'recents' && block.rows.length === 0" class="sidebar-zone-empty">
          <div class="sidebar-zone-empty__body">{{ t('shared.sidebar.noConversations') }}</div>
        </div>
        </div>
          <div
            :id="`sidebar-group-${block.key}`"
            class="sidebar-group__body"
          >
            <div class="sidebar-group__content">
              <template v-for="{ row, index, gapBefore } in rows" :key="row.key">
              <div v-if="gapBefore" class="sidebar-virtual-spacer" :style="{ height: `${gapBefore}px` }" aria-hidden="true" />
              <div
                :ref="measureSidebarItem"
                :data-index="index"
                :data-sidebar-item-key="`row:${row.key}`"
                class="sidebar-virtual-row"
              >
              <div
                class="sidebar-history-row"
                :class="{
                  'is-selected': row.rowKind === 'session' && isRowSelected(row.key),
                  'sidebar-history-row--workspace': row.rowKind === 'workspace',
                  'sidebar-history-row--workspace-empty': row.rowKind === 'workspace-empty',
                  'sidebar-history-row--subtask': (taskHierarchy.ancestors.get(row.key)?.length ?? 0) > 0,
                  'is-unavailable': row.rowKind === 'workspace' && row.workspaceAvailable === false,
                  'is-reorderable': canDragRow(row),
                  'is-dragging': draggedRowKey === row.key,
                  'is-settling': settlingRowKey === row.key,
                  'is-drop-before': dropTargetKey === row.key && dropPosition === 'before',
                  'is-drop-after': dropTargetKey === row.key && dropPosition === 'after',
                  'has-subtasks': taskHierarchy.summaries.has(row.key),
                }"
                :data-family="row.displayFamily"
                :data-sidebar-zone="row.displayZone"
                :data-depth="row.depth"
                :data-session-key="row.rowKind === 'session' ? row.key : undefined"
                :style="{ '--row-depth': row.depth }"
                @pointerdown="onRowPointerDown(row, $event)"
                @mouseenter="openSessionPreview(row, $event)"
                @mouseleave="closeSessionPreview"
                @focusin="openSessionPreview(row, $event)"
                @focusout="onSessionFocusOut"
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

                <button
                  v-if="row.rowKind === 'session' && taskHierarchy.summaries.has(row.key)"
                  type="button"
                  class="sidebar-task-disclosure"
                  :aria-expanded="!isTaskCollapsed(row.key)"
                  :aria-label="t(isTaskCollapsed(row.key) ? 'shared.sidebar.expandSubtasks' : 'shared.sidebar.collapseSubtasks', { title: row.title })"
                  @click.stop="toggleTask(row)"
                >
                  <Icon name="chevronRight" :size="12" />
                </button>

                <!-- Inline rename input replaces the row button while editing -->
                <input
                  v-if="row.rowKind === 'session' && renamingKey === row.key"
                  :ref="setRenameInput"
                  v-model="renameDraft"
                  class="sidebar-history-rename"
                  type="text"
                  :aria-label="t('shared.sidebar.renameLabel', { title: row.title })"
                  @keydown.enter.prevent="commitRename(true)"
                  @keydown.esc.prevent="cancelRename()"
                  @blur="onRenameBlur"
                />

                <button
                  v-else-if="row.rowKind === 'session'"
                  class="sidebar-history-item"
                  :class="{ 'is-current': row.key === currentKey }"
                  :aria-current="row.key === currentKey ? 'page' : undefined"
                  :aria-pressed="selectionMode && !row.provisional ? isRowSelected(row.key) : undefined"
                  :aria-describedby="sessionPreview?.row.key === row.key ? 'sidebar-session-preview' : undefined"
                  @click="onSelectRow(row, $event)"
                  @contextmenu="openSessionContextMenu(row, $event)"
                >
                  <span
                    v-if="selectionMode && !row.provisional"
                    class="sidebar-selection-box"
                    :class="{ 'is-checked': isRowSelected(row.key) }"
                    aria-hidden="true"
                  >
                    <Icon v-if="isRowSelected(row.key)" name="check" :size="11" />
                  </span>
                  <span class="sidebar-history-main">
                    <span class="sidebar-history-title">{{ row.title }}</span>
                    <span
                      v-if="taskHierarchy.summaries.has(row.key) && !selectionMode"
                      class="sidebar-subtask-summary"
                      :class="{ 'has-attention': taskHierarchy.summaries.get(row.key)?.attention }"
                      :title="subtaskSummaryLabel(row.key)"
                    >
                      <span class="sidebar-subtask-count">{{ t('shared.sidebar.subtaskCount', { count: taskHierarchy.summaries.get(row.key)?.count }) }}</span>
                      <span
                        v-if="taskHierarchy.summaries.get(row.key)?.attention"
                        class="sidebar-subtask-status sidebar-subtask-status--attention"
                        role="img"
                        :aria-label="t('shared.sidebar.subtasksAttention', { count: taskHierarchy.summaries.get(row.key)?.attention })"
                      >
                        <Icon name="info" :size="11" aria-hidden="true" />
                        <span aria-hidden="true">{{ taskHierarchy.summaries.get(row.key)?.attention }}</span>
                      </span>
                      <span
                        v-if="taskHierarchy.summaries.get(row.key)?.running"
                        class="sidebar-subtask-status"
                        role="img"
                        :aria-label="t('shared.sidebar.subtasksRunning', { count: taskHierarchy.summaries.get(row.key)?.running })"
                      >
                        <Icon name="refresh" :size="11" aria-hidden="true" />
                        <span aria-hidden="true">{{ taskHierarchy.summaries.get(row.key)?.running }}</span>
                      </span>
                    </span>
                  </span>
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

                <!-- Per-session ⋯ menu: task rows omit pin but keep rename + delete. -->
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
                  v-if="canOpenSessionMenu(row)"
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
                      v-if="row.sessionKind !== 'task'"
                      type="button"
                      class="sidebar-row-menu__item"
                      role="menuitem"
                      @click.stop="emitSessionPin(row)"
                    >
                      <Icon name="arrowUp" :size="14" />
                      <span>{{ row.pinned ? t('shared.sidebar.unpinTask') : t('shared.sidebar.pinTask') }}</span>
                    </button>
                    <button
                      v-if="keyboardReorderTarget(row, 'up')"
                      type="button"
                      class="sidebar-row-menu__item"
                      role="menuitem"
                      data-session-action="move-up"
                      @click.stop="reorderFromMenu(row, 'up')"
                    >
                      <Icon name="arrowUp" :size="14" />
                      <span>{{ t('shared.sidebar.moveUp') }}</span>
                    </button>
                    <button
                      v-if="keyboardReorderTarget(row, 'down')"
                      type="button"
                      class="sidebar-row-menu__item"
                      role="menuitem"
                      data-session-action="move-down"
                      @click.stop="reorderFromMenu(row, 'down')"
                    >
                      <Icon class="sidebar-move-down" name="arrowUp" :size="14" />
                      <span>{{ t('shared.sidebar.moveDown') }}</span>
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
                    && shouldShowAgentFilterBadge(row.displayFamily, row)
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
              </div>
              </template>
              <div v-if="gapAfter" class="sidebar-virtual-spacer" :style="{ height: `${gapAfter}px` }" aria-hidden="true" />
            </div>
          </div>
      </div>
      <div
        v-if="loadingMore || loadMoreError || (!hasMore && totalRows > 0)"
        class="sidebar-history-page-state"
        role="status"
      >
        <span v-if="loadingMore">{{ t('sessions.loading') }}</span>
        <button
          v-else-if="loadMoreError"
          type="button"
          class="sidebar-history-retry"
          @click="emit('load-more')"
        >
          {{ t('shared.errorState.retry') }}
        </button>
        <span v-else>{{ t('shared.sidebar.allLoaded') }}</span>
      </div>
    </div>
    <Teleport to="body">
      <SidebarSessionDragPreview v-if="pointerDrag?.active" :drag="pointerDrag" />
      <SidebarSessionHoverCard
        v-if="sessionPreview"
        :title="sessionPreview.row.title"
        :updated-at="sessionPreview.row.updatedAt"
        :project-name="sessionPreview.row.displayProjectName"
        :position="sessionPreview.position"
      />
    </Teleport>
  </div>
</template>

<style scoped>
.sidebar-history-list {
  padding-top: 0;
}

.sidebar-history-list[data-sidebar-virtualized='true'] {
  overflow-anchor: none;
}

.sidebar-group + .sidebar-group {
  margin-top: 0;
}

/* Include group spacing and row margins in TanStack's measured boxes. */
.sidebar-virtual-heading {
  display: flow-root;
  padding-top: var(--sp-1);
}

.sidebar-virtual-row {
  display: flow-root;
}

.sidebar-virtual-spacer {
  flex: 0 0 auto;
  pointer-events: none;
}

.sidebar-move-down {
  transform: rotate(180deg);
}

.sidebar-history-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.sidebar-subtask-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--sidebar-text-soft);
  font-size: 0.6875rem;
  font-weight: 400;
  line-height: 1.35;
}

.sidebar-subtask-count {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar-subtask-status {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex: 0 0 auto;
}

.sidebar-subtask-status--attention {
  color: var(--warn);
}

.sidebar-task-disclosure {
  position: absolute;
  z-index: 1;
  left: calc(var(--row-depth, 0) * 14px);
  display: grid;
  place-items: center;
  width: 24px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--sidebar-text-soft);
  cursor: pointer;
}

.sidebar-task-disclosure:hover {
  color: var(--sidebar-text-strong);
  background: var(--sidebar-item-hover);
}

.sidebar-task-disclosure:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.sidebar-task-disclosure[aria-expanded='true'] :deep(svg) {
  transform: rotate(90deg);
}

.has-subtasks:has(.sidebar-task-disclosure) .sidebar-history-item,
.sidebar-history-row--subtask .sidebar-history-item {
  padding-left: 28px;
}

@media (pointer: coarse) {
  .sidebar-task-disclosure {
    width: 32px;
    height: 44px;
  }

  .has-subtasks:has(.sidebar-task-disclosure) .sidebar-history-item,
  .sidebar-history-row--subtask .sidebar-history-item {
    min-height: 48px;
    padding-left: 34px;
  }
}
</style>
